import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, Any
from urllib.parse import urlparse

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
    server_host: str
    server_port: Optional[int]
    server_use_ssl: bool
    bind_dn: str
    bind_password: str
    search_base: str
    search_filter: str
    domain: Optional[str]
    connect_timeout_seconds: float
    receive_timeout_seconds: float
    ip_mode: str


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


def _ldap3_receive_timeout(value: float) -> int:
    # ldap3 2.9.1 eventually packs this into a native timeval struct and
    # raises struct.error if given a float.
    return max(1, int(value))


def _parse_server_uri(server_uri: str) -> Tuple[str, Optional[int], bool]:
    parsed = urlparse(server_uri)
    # Accept full URIs (ldap://host:389, ldaps://host:636) and bare host[:port].
    if parsed.scheme:
        if parsed.scheme not in {"ldap", "ldaps"}:
            raise RuntimeError("LDAP_SERVER must use ldap:// or ldaps://")
        if not parsed.hostname:
            raise RuntimeError("LDAP_SERVER must include a hostname")
        use_ssl = parsed.scheme == "ldaps"
        default_port = 636 if use_ssl else 389
        return parsed.hostname, parsed.port or default_port, use_ssl
    if ":" in server_uri:
        host, port_text = server_uri.rsplit(":", 1)
        if not host:
            raise RuntimeError("LDAP_SERVER hostname is empty")
        try:
            return host, int(port_text), False
        except ValueError as exc:
            raise RuntimeError("LDAP_SERVER port must be an integer") from exc
    return server_uri, None, False


def _load_ip_mode() -> str:
    value = os.getenv("LDAP_IP_MODE", "v4_only").strip().lower()
    mapping = {
        "v4_only": "IP_V4_ONLY",
        "v4_preferred": "IP_V4_PREFERRED",
        "v6_only": "IP_V6_ONLY",
        "v6_preferred": "IP_V6_PREFERRED",
    }
    mode = mapping.get(value)
    if not mode:
        raise RuntimeError(
            "LDAP_IP_MODE must be one of: v4_only, v4_preferred, v6_only, v6_preferred"
        )
    return mode


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
    server_host, server_port, server_use_ssl = _parse_server_uri(server_uri)
    ip_mode = _load_ip_mode()

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
        server_host=server_host,
        server_port=server_port,
        server_use_ssl=server_use_ssl,
        bind_dn=bind_dn,
        bind_password=bind_password,
        search_base=search_base,
        search_filter=search_filter,
        domain=domain,
        connect_timeout_seconds=connect_timeout_seconds,
        receive_timeout_seconds=receive_timeout_seconds,
        ip_mode=ip_mode,
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
        import ldap3

        _server = Server(
            config.server_host,
            port=config.server_port,
            use_ssl=config.server_use_ssl,
            connect_timeout=config.connect_timeout_seconds,
            mode=getattr(ldap3, config.ip_mode),
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
        receive_timeout = _ldap3_receive_timeout(config.receive_timeout_seconds)
        logger.info("[ldap] connecting to LDAP server for service bind...")
        _service_conn = Connection(
            server,
            user=config.bind_dn,
            password=config.bind_password,
            auto_bind=True,
            receive_timeout=receive_timeout,
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
    receive_timeout = _ldap3_receive_timeout(config.receive_timeout_seconds)
    try:
        user_conn = Connection(
            server,
            user=dn,
            password=password,
            auto_bind=True,
            receive_timeout=receive_timeout,
        )
        user_conn.unbind()
        return True
    except Exception:
        return False
