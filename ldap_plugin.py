import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, Any

logger = logging.getLogger("proxy")

_lock = threading.Lock()
_server = None
_service_conn = None


def ldap_enabled() -> bool:
    return os.getenv("USE_LDAP", "0").strip().lower() in {"1", "true", "yes", "on"}


def _ldap3_imports() -> Tuple[Any, Any, Any, Any]:
    try:
        from ldap3 import Server, Connection, SUBTREE
        from ldap3.utils.conv import escape_filter_chars
    except Exception as exc:
        raise RuntimeError("ldap3 must be installed to use LDAP authentication") from exc
    return Server, Connection, SUBTREE, escape_filter_chars


@dataclass(frozen=True)
class LdapConfig:
    server_uri: str
    bind_dn: str
    bind_password: str
    search_base: str
    search_filter: str
    domain: Optional[str]
    connect_timeout_seconds: float
    receive_timeout_seconds: float


def _load_timeout_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number of seconds") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0")
    return value


def _load_config() -> LdapConfig:
    server_uri = os.getenv("LDAP_SERVER", "").strip()
    bind_dn = os.getenv("LDAP_DN", "").strip()
    bind_password = os.getenv("LDAP_PW", "").strip()
    search_base = os.getenv("LDAP_SEARCH_BASE", "").strip()
    search_filter = os.getenv(
        "LDAP_FILTER", "(&(objectClass=person)(userPrincipalName=%s))"
    ).strip()
    domain = os.getenv("LDAP_DOMAIN", "").strip() or None
    connect_timeout_seconds = _load_timeout_env("LDAP_CONNECT_TIMEOUT_SECONDS", 5.0)
    receive_timeout_seconds = _load_timeout_env("LDAP_RECEIVE_TIMEOUT_SECONDS", 10.0)

    missing = [
        name
        for name, value in (
            ("LDAP_SERVER", server_uri),
            ("LDAP_DN", bind_dn),
            ("LDAP_PW", bind_password),
            ("LDAP_SEARCH_BASE", search_base),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"LDAP is enabled but missing settings: {', '.join(missing)}")
    if "%s" not in search_filter:
        raise RuntimeError("LDAP_FILTER must contain a %s placeholder for the username")
    return LdapConfig(
        server_uri=server_uri,
        bind_dn=bind_dn,
        bind_password=bind_password,
        search_base=search_base,
        search_filter=search_filter,
        domain=domain,
        connect_timeout_seconds=connect_timeout_seconds,
        receive_timeout_seconds=receive_timeout_seconds,
    )


def _normalize_username(config: LdapConfig, username: str) -> str:
    value = (username or "").strip()
    if not value:
        return value
    if config.domain and "@" not in value:
        if config.domain.startswith("@"):
            value += config.domain
        else:
            value += f"@{config.domain}"
    return value


def _get_server(config: LdapConfig):
    global _server
    if _server is None:
        Server, _, _, _ = _ldap3_imports()
        _server = Server(
            config.server_uri,
            connect_timeout=config.connect_timeout_seconds,
        )
    return _server


def _get_service_connection(config: LdapConfig):
    global _service_conn
    if _service_conn is not None and _service_conn.bound:
        return _service_conn
    with _lock:
        if _service_conn is not None and _service_conn.bound:
            return _service_conn
        _, Connection, _, _ = _ldap3_imports()
        server = _get_server(config)
        logger.info("[ldap] connecting to LDAP server for service bind...")
        _service_conn = Connection(
            server,
            user=config.bind_dn,
            password=config.bind_password,
            auto_bind=True,
            receive_timeout=config.receive_timeout_seconds,
        )
        logger.info("[ldap] LDAP service bind established")
        return _service_conn


def _drop_service_connection() -> None:
    global _service_conn
    with _lock:
        if _service_conn is not None:
            try:
                _service_conn.unbind()
            except Exception:
                pass
        _service_conn = None


def _find_user_dn(config: LdapConfig, username: str) -> Optional[str]:
    _, _, SUBTREE, escape_filter_chars = _ldap3_imports()
    normalized = _normalize_username(config, username)
    if not normalized:
        return None
    safe_username = escape_filter_chars(normalized)
    search_filter = config.search_filter % safe_username
    # Retry once when an existing service connection goes stale.
    for attempt in (1, 2):
        conn = _get_service_connection(config)
        try:
            with _lock:
                ok = conn.search(
                    search_base=config.search_base,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=["cn"],
                )
                if not ok or not conn.entries:
                    return None
                return conn.entries[0].entry_dn
        except Exception as exc:
            logger.warning("[ldap] service search failed on attempt %s: %s", attempt, exc)
            _drop_service_connection()
            if attempt == 2:
                raise RuntimeError("LDAP service search failed") from exc
    return None


def authenticate(username: str, password: str) -> bool:
    if not ldap_enabled():
        return True
    if not password:
        return False
    config = _load_config()
    dn = _find_user_dn(config, username)
    if not dn:
        return False
    _, Connection, _, _ = _ldap3_imports()
    server = _get_server(config)
    try:
        user_conn = Connection(
            server,
            user=dn,
            password=password,
            auto_bind=True,
            receive_timeout=config.receive_timeout_seconds,
        )
        user_conn.unbind()
        return True
    except Exception:
        return False
