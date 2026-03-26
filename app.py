import html
import logging
import os
from urllib.parse import quote
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

base_dir = os.path.dirname(os.path.abspath(__file__))

# Load environment from local files before reading variables (works regardless of CWD).
# Prefer .env.local over any pre-set environment values for local dev.
load_dotenv(os.path.join(base_dir, ".env.local"), override=True)
load_dotenv(os.path.join(base_dir, ".env"), override=False)

# Logging to file for integration debugging.
LOG_PATH = os.getenv("PROXY_LOG_PATH", "./data/proxy.log")
log_dir = os.path.dirname(LOG_PATH)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)
logger = logging.getLogger("proxy")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, mode="w")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

logger.info(
    "[config] UPSTREAM_BASE=%s UPSTREAM_API_VERSION=%s",
    os.getenv("UPSTREAM_BASE"),
    os.getenv("UPSTREAM_API_VERSION"),
)

import ldap_plugin  # noqa: E402

from db_schema import ensure_expected_schema  # noqa: E402
from identity import canonicalize_username  # noqa: E402
from tracking import (  # noqa: E402
    DATABASE_PATH,
    SESSION_TTL_SECONDS,
    create_api_key,
    create_session,
    fetch_active_keys_for_label,
    fetch_report_rows,
    fetch_report_summary,
    fetch_report_users,
    fetch_usage_summary,
    get_active_session,
    init_db,
    revoke_session,
    revoke_key,
)

from proxy import (  # noqa: E402
    ensure_models_loaded,
    get_debug_state,
    get_models_snapshot,
    refresh_models_cache,
    router as proxy_router,
)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")  # Simple shared secret for managing keys
SESSION_COOKIE_NAME = "proxy_session"


def require_admin(admin_token: str):
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=500, detail="ADMIN_TOKEN must be set to manage keys"
        )
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


app = FastAPI(title="OpenAI Proxy")
app.include_router(proxy_router)


def mask_token(token: str) -> str:
    if len(token) <= 8:
        return token
    return f"{token[:4]}…{token[-4:]}"


def parse_date_range(start: Optional[str], end: Optional[str]) -> Tuple[int, int]:
    """
    Accepts YYYY-MM-DD strings and returns epoch seconds (inclusive start, exclusive end).
    Defaults to last 30 days ending today (start at 00:00, end at 00:00 next day).
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone.utc
    today = datetime.now(tz=tz).date()
    if end:
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    else:
        end_date = today
    if start:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
    else:
        start_date = end_date - timedelta(days=30)

    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(
        tzinfo=tz
    )
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def request_is_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower() == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: RedirectResponse, request: Request, session_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_secure(request),
    )
    response.delete_cookie("proxy_username")


def clear_session_cookie(response: RedirectResponse, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=request_is_secure(request),
    )
    response.delete_cookie("proxy_username")


def resolve_session(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_active_session(session_token or "")
    if session:
        return session
    raise HTTPException(status_code=401, detail="Login required")


def resolve_username(request: Request) -> str:
    return str(resolve_session(request)["username"])


def require_username(request: Request) -> str:
    return resolve_username(request)


def build_login_html(
    target: str, ldap_enabled: bool, error_message: Optional[str] = None
) -> str:
    error_block = ""
    if error_message:
        error_block = f'<div class="error">{html.escape(error_message)}</div>'
    password_block = ""
    login_copy = "No passwords yet. Enter a username to start a signed-in session."
    login_hint = "We store an opaque <code>proxy_session</code> cookie for local navigation."
    if ldap_enabled:
        login_copy = "Use your LDAP username and password to sign in."
        login_hint = "Credentials are verified against LDAP before setting your session."
        password_block = """
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required />
"""
    content = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Proxy Login</title>
  <style>
    :root {
      --bg: radial-gradient(circle at 10% 20%, #0f172a 0, #0a1224 30%, #050a16 70%, #020610 100%);
      --panel: rgba(255, 255, 255, 0.06);
      --border: rgba(255,255,255,0.08);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #22d3ee;
      --accent-2: #a855f7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Space Grotesk","Manrope","Soehne",system-ui,-apple-system,sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }
    .card {
      width: 100%;
      max-width: 520px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 26px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.45);
      backdrop-filter: blur(12px);
    }
    h1 { margin: 0 0 10px; letter-spacing: -0.02em; }
    p { margin: 0 0 18px; color: var(--muted); }
    label { display:block; margin-bottom:6px; font-weight:700; }
    input {
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.05);
      color: var(--text);
      font-size: 15px;
      margin-bottom: 12px;
    }
    button {
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: none;
      font-weight: 800;
      cursor: pointer;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      color: #0b1225;
      box-shadow: 0 8px 30px rgba(34, 211, 238, 0.25);
      transition: transform 120ms ease, box-shadow 120ms ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 12px 36px rgba(34, 211, 238, 0.35); }
    button:active { transform: translateY(0); }
    .hint { font-size: 14px; color: var(--muted); margin-top: 8px; }
    .error {
      background: rgba(248, 113, 113, 0.12);
      border: 1px solid rgba(248, 113, 113, 0.35);
      color: #fecaca;
      padding: 10px 12px;
      border-radius: 12px;
      margin-bottom: 12px;
      font-size: 14px;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in</h1>
    <p>__LOGIN_COPY__</p>
    __ERROR__
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="__NEXT__" />
      <label for="username">Username</label>
      <input id="username" name="username" placeholder="e.g. first.last" autocomplete="username" required />
__PASSWORD__
      <button type="submit">Continue</button>
      <p class="hint">__LOGIN_HINT__</p>
    </form>
  </div>
  <script>
    // Autofocus username field for faster sign-in.
    const input = document.getElementById('username');
    if (input) input.focus();
  </script>
</body>
</html>
    """
    return (
        content.replace("__NEXT__", html.escape(target))
        .replace("__PASSWORD__", password_block)
        .replace("__ERROR__", error_block)
        .replace("__LOGIN_COPY__", login_copy)
        .replace("__LOGIN_HINT__", login_hint)
    )


def login_redirect(request: Request) -> RedirectResponse:
    """Redirect to login with a next parameter pointing back to the requested path."""
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(next_path)}", status_code=302)


def sanitized_next(target: Optional[str]) -> str:
    """Allow only relative paths for post-login redirects to avoid open redirects."""
    if target and target.startswith("/"):
        return target
    return "/"


@app.on_event("startup")
async def on_startup():
    ensure_expected_schema(DATABASE_PATH)
    init_db()
    try:
        await refresh_models_cache()
        logger.info(
            "[models] cached %s models",
            len(get_models_snapshot()[0]),
        )
    except Exception as exc:
        logger.warning("[models] startup fetch failed: %s", repr(exc))


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/debug/last-response")
async def debug_last_response(request: Request):
    require_username(request)
    return get_debug_state()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Optional[str] = "/"):
    target = sanitized_next(next)
    try:
        resolve_session(request)
        return RedirectResponse(url=target, status_code=302)
    except HTTPException:
        pass
    ldap_enabled = ldap_plugin.ldap_enabled()
    return HTMLResponse(build_login_html(target, ldap_enabled))


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: Optional[str] = Form(None),
    next: str = Form("/"),
):
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    canonical_username = canonicalize_username(username)
    if not canonical_username:
        raise HTTPException(status_code=400, detail="username is required")
    target = sanitized_next(next)
    ldap_enabled = ldap_plugin.ldap_enabled()
    if ldap_enabled:
        password = (password or "").strip()
        if not password:
            return HTMLResponse(
                build_login_html(
                    target, ldap_enabled, error_message="Password is required."
                ),
                status_code=400,
            )
        try:
            if not ldap_plugin.authenticate(username, password):
                return HTMLResponse(
                    build_login_html(
                        target,
                        ldap_enabled,
                        error_message="Invalid username or password.",
                    ),
                    status_code=401,
                )
        except RuntimeError as exc:
            logger.exception("[ldap] authentication failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    session_token = create_session(canonical_username)
    response = RedirectResponse(url=target, status_code=303)
    set_session_cookie(response, request, session_token)
    return response


@app.post("/logout")
async def logout(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        revoke_session(session_token)
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response, request)
    return response


@app.get("/models")
async def list_models(request: Request):
    """
    Fetch available models from the upstream /models endpoint.
    """
    require_username(request)
    await ensure_models_loaded()
    models, loaded_at = get_models_snapshot()
    if not models:
        raise HTTPException(status_code=502, detail="Models cache is empty")
    return {"models": models, "loaded_at": loaded_at}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    try:
        username = require_username(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return login_redirect(request)
        raise
    content = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Proxy Portal</title>
  <style>
    body {
      margin: 0;
      font-family: "Space Grotesk","Manrope","Soehne",system-ui,-apple-system,sans-serif;
      background: radial-gradient(circle at 10% 20%, #0f172a 0, #0a1224 30%, #050a16 70%, #020610 100%);
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }
    .card {
      width: 100%;
      max-width: 640px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 20px;
      padding: 28px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.45);
      backdrop-filter: blur(12px);
    }
    h1 { margin: 0 0 12px; letter-spacing: -0.02em; }
    p { margin: 0 0 24px; color: #94a3b8; }
    .links { display: grid; grid-template-columns: 1fr; gap: 14px; }
    a {
      display: block;
      text-decoration: none;
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: #e2e8f0;
      font-weight: 600;
      transition: transform 120ms ease, box-shadow 120ms ease;
      box-shadow: 0 8px 30px rgba(34, 211, 238, 0.15);
    }
    a:hover { transform: translateY(-1px); box-shadow: 0 12px 36px rgba(34, 211, 238, 0.25); }
  </style>
</head>
<body>
  <div class="card">
    <h1>Proxy Portal</h1>
    <p>Signed in as <strong>__USERNAME__</strong>. Request keys and view cost reports for your team.</p>
    <div class="links">
      <a href="/request-key">Request or view keys</a>
      <a href="/reports">Cost reports</a>
      <form method="POST" action="/logout"><button type="submit" style="width:100%; text-align:left; padding:14px 16px; border-radius:14px; border:1px solid rgba(255,255,255,0.08); background:rgba(255,255,255,0.04); color:#e2e8f0; font-weight:600; cursor:pointer;">Log out</button></form>
    </div>
  </div>
</body>
</html>
        """
    content = content.replace("__USERNAME__", html.escape(username))
    return HTMLResponse(content)


@app.post("/admin/keys")
async def issue_key(payload: Dict[str, str], x_admin_token: str = Header(...)):
    require_admin(x_admin_token)
    label = canonicalize_username(payload.get("label") or "user")
    note = payload.get("note")
    token = create_api_key(label, note=note)
    return {"token": token, "label": label, "note": note}


@app.post("/admin/keys/{token}/revoke")
async def revoke(token: str, x_admin_token: str = Header(...)):
    require_admin(x_admin_token)
    if revoke_key(token):
        return {"status": "revoked", "token": token}
    raise HTTPException(status_code=404, detail="Token not found or already revoked")


@app.get("/admin/usage/{token}")
async def usage_for_token(token: str, x_admin_token: str = Header(...)):
    require_admin(x_admin_token)
    rows = fetch_usage_summary(token)
    return {
        "token": token,
        "models": rows,
    }


@app.get("/reports/data")
async def reports_data(
    start: Optional[str] = None,
    end: Optional[str] = None,
    scope: str = "all_users",
    bucket: str = "day",
    user: Optional[str] = None,
    request: Request = None,
):
    if request:
        require_username(request)
    valid_scopes = {"all_users", "user", "key", "user_key"}
    valid_buckets = {"all", "year", "month", "week", "day"}
    if scope not in valid_scopes:
        raise HTTPException(status_code=400, detail=f"Invalid scope '{scope}'")
    if bucket not in valid_buckets:
        raise HTTPException(status_code=400, detail=f"Invalid bucket '{bucket}'")

    if bucket == "all":
        start_ts, end_ts = 0, 32503680000  # UTC 3000-01-01
    else:
        start_ts, end_ts = parse_date_range(start, end)

    rows = fetch_report_rows(
        start_ts=start_ts,
        end_ts=end_ts,
        bucket=bucket,
        scope=scope,
        username=user,
    )
    summary = fetch_report_summary(start_ts=start_ts, end_ts=end_ts, username=user)

    return {
        "query": {
            "scope": scope,
            "bucket": bucket,
            "user": user,
            "start": start,
            "end": end,
        },
        "period": {
            "start": start_ts,
            "end": end_ts,
        },
        "users": fetch_report_users(),
        "rows": rows,
        "summary": summary,
    }


@app.post("/public/keys")
async def public_issue_key(payload: Dict[str, str], request: Request):
    username = require_username(request)
    note = (payload.get("note") or "").strip() or None
    token = create_api_key(username, note=note)
    return {"token": token, "username": username, "note": note, "masked": mask_token(token)}


@app.get("/public/keys/{username}")
async def public_list_keys(username: str, request: Request):
    current_username = require_username(request)
    requested_username = canonicalize_username(username)
    if requested_username and requested_username != current_username:
        raise HTTPException(status_code=403, detail="Cannot list keys for another user")
    rows = fetch_active_keys_for_label(current_username)
    return {
        "username": current_username,
        "keys": [
            {
                "token_masked": mask_token(r["token"]),
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/request-key", response_class=HTMLResponse)
async def request_key_page(request: Request):
    try:
        username = require_username(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return login_redirect(request)
        raise
    content = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Request API Key</title>
  <style>
    :root {
      --bg: radial-gradient(circle at 10% 20%, #0f172a 0, #0a1224 30%, #050a16 70%, #020610 100%);
      --panel: rgba(255, 255, 255, 0.06);
      --accent: #22d3ee;
      --accent-2: #a855f7;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --border: rgba(255,255,255,0.08);
      --shadow: 0 20px 60px rgba(0,0,0,0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Space Grotesk", "Manrope", "Soehne", system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }
    .card {
      width: 100%;
      max-width: 780px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 28px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }
    h1 {
      margin: 0 0 12px 0;
      letter-spacing: -0.02em;
      font-size: 24px;
    }
    p { margin: 0 0 18px 0; color: var(--muted); }
    label { display: block; margin: 12px 0 6px; font-weight: 600; }
    input, textarea, button {
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      font-size: 15px;
    }
    textarea { resize: vertical; min-height: 80px; }
    button {
      margin-top: 12px;
      font-weight: 700;
      cursor: pointer;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      border: none;
      transition: transform 120ms ease, box-shadow 120ms ease;
      box-shadow: 0 8px 30px rgba(34, 211, 238, 0.25);
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 12px 36px rgba(34, 211, 238, 0.35); }
    button:active { transform: translateY(0); }
    .status { margin-top: 12px; font-size: 14px; color: var(--muted); }
    .keys {
      margin-top: 18px;
      padding: 14px;
      border: 1px dashed var(--border);
      border-radius: 12px;
      background: rgba(255,255,255,0.03);
    }
    .key-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 0;
      border-bottom: 1px solid var(--border);
    }
    .key-row:last-child { border-bottom: none; }
    .key-mask { font-family: "SFMono-Regular", ui-monospace, monospace; color: var(--text); }
    .note { color: var(--muted); font-size: 13px; }
    .pill { padding: 4px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); color: var(--muted); font-size: 12px; }
    .token-full {
      margin-top: 12px;
      padding: 12px;
      border-radius: 12px;
      background: rgba(34, 211, 238, 0.08);
      border: 1px solid rgba(34, 211, 238, 0.35);
      font-family: "SFMono-Regular", ui-monospace, monospace;
      word-break: break-all;
    }
    .nav { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
    .pill-link { padding:6px 10px; border-radius:999px; background:rgba(255,255,255,0.06); border:1px solid var(--border); text-decoration:none; color:var(--text); font-size:13px; }
    .logout-btn { padding:6px 10px; border-radius:999px; background:rgba(255,255,255,0.06); border:1px solid var(--border); color:var(--text); font-size:13px; cursor:pointer; }
  </style>
</head>
<body>
  <div class="card">
    <div class="nav">
      <a class="pill-link" href="/">Home</a>
      <form method="POST" action="/logout" style="margin:0;"><button class="logout-btn" type="submit">Log out</button></form>
    </div>
    <h1>Get an API key</h1>
    <p>Signed in as <strong>__USERNAME__</strong>. Add a note about what you’re using the key for. New keys are UUID hex strings; keep them secret.</p>
    <form id="key-form">
      <label for="note">Note (what is this key for?)</label>
      <textarea id="note" name="note" placeholder="team tool, prototype, etc."></textarea>
      <button type="submit">Request key</button>
    </form>
    <div id="status" class="status"></div>
    <div id="token" class="token-full" style="display:none;"></div>
    <div class="keys">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <strong>Your active keys</strong>
        <span class="pill" id="key-count">0</span>
      </div>
      <div id="keys-list"></div>
    </div>
  </div>
  <div id="root-data" data-username="__USERNAME__"></div>
  <script>
    const form = document.getElementById('key-form');
    const statusEl = document.getElementById('status');
    const tokenEl = document.getElementById('token');
    const listEl = document.getElementById('keys-list');
    const countEl = document.getElementById('key-count');
    const username = document.getElementById('root-data').dataset.username;

    async function fetchKeys(username) {
      const res = await fetch(`/public/keys/${encodeURIComponent(username)}`);
      if (!res.ok) throw new Error('Unable to load keys');
      return res.json();
    }

    function renderKeys(payload) {
      listEl.innerHTML = '';
      const keys = payload.keys || [];
      countEl.textContent = keys.length;
      if (keys.length === 0) {
        listEl.innerHTML = '<div class="note">No keys yet.</div>';
        return;
      }
      keys.forEach(k => {
        const row = document.createElement('div');
        row.className = 'key-row';
        const left = document.createElement('div');
        left.innerHTML = `<div class="key-mask">${k.token_masked}</div><div class="note">${k.note || '—'}</div>`;
        const right = document.createElement('div');
        const date = new Date((k.created_at || 0) * 1000);
        right.innerHTML = `<span class="pill">${date.toLocaleString()}</span>`;
        row.appendChild(left);
        row.appendChild(right);
        listEl.appendChild(row);
      });
    }

    async function loadKeys() {
      if (!username) return;
      try {
        const payload = await fetchKeys(username);
        renderKeys(payload);
      } catch (err) {
        statusEl.textContent = err.message;
      }
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const note = form.note.value.trim();
      if (!username) {
        statusEl.textContent = 'Username is missing.';
        return;
      }
      statusEl.textContent = 'Creating key…';
      tokenEl.style.display = 'none';
      try {
        const res = await fetch('/public/keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, note })
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || 'Request failed');
        }
        const payload = await res.json();
        statusEl.textContent = 'Key created. Copy it now—this is the only time it will be shown.';
        tokenEl.textContent = payload.token;
        tokenEl.style.display = 'block';
        await loadKeys();
      } catch (err) {
        statusEl.textContent = err.message || 'Error creating key.';
      }
    });

    loadKeys();
  </script>

</body>
</html>
        """
    return HTMLResponse(content.replace("__USERNAME__", html.escape(username)))


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    try:
        require_username(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return login_redirect(request)
        raise
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Usage Reports</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: radial-gradient(circle at 12% 12%, #102341 0, #0a1830 30%, #081224 60%, #04080f 100%);
      --panel: rgba(255,255,255,0.07);
      --panel-strong: rgba(255,255,255,0.11);
      --border: rgba(255,255,255,0.09);
      --text: #e5ecf4;
      --muted: #9aa9bd;
      --accent: #38bdf8;
      --accent-2: #fb7185;
      --ok: #34d399;
      --warn: #f59e0b;
      --mono: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "Space Grotesk","Manrope","Soehne",system-ui,-apple-system,sans-serif;
      padding: 22px;
    }
    .layout {
      max-width: 1360px;
      margin: 0 auto;
      display: grid;
      gap: 16px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 20px 55px rgba(0,0,0,0.35);
      backdrop-filter: blur(10px);
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .top h1 { margin: 0; font-size: 26px; letter-spacing: -0.02em; }
    .nav { display: flex; gap: 8px; flex-wrap: wrap; }
    .pill-link, .logout-btn {
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.07);
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      cursor: pointer;
    }
    .desc { margin: 8px 0 0 0; color: var(--muted); font-size: 14px; }
    .control-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
    }
    label {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    select, input, button {
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.05);
      color: var(--text);
      font-size: 14px;
    }
    button.primary {
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      border: none;
      color: #0a1020;
      font-weight: 700;
      cursor: pointer;
    }
    .range-pills {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .range-pills button {
      width: auto;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      cursor: pointer;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
    }
    .stat {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      background: var(--panel-strong);
    }
    .stat .k {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .stat .v {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .pill {
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
    }
    .viz-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 14px;
    }
    .viz-wide { grid-column: 1 / -1; }
    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }
    .title-row h3 { margin: 0; }
    .table-wrap { overflow: auto; max-height: 420px; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 9px 8px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      white-space: nowrap;
    }
    th { color: var(--muted); font-weight: 700; position: sticky; top: 0; background: rgba(6, 11, 20, 0.9); }
    td.right { text-align: right; font-variant-numeric: tabular-nums; }
    .status {
      color: var(--muted);
      font-size: 13px;
      margin-top: 8px;
      min-height: 18px;
    }
    .mono { font-family: var(--mono); }
    @media (max-width: 860px) {
      body { padding: 12px; }
      .card { padding: 12px; }
      .stat .v { font-size: 18px; }
      canvas { max-height: 280px; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <section class="card">
      <div class="top">
        <h1>Usage reports</h1>
        <div class="nav">
          <a class="pill-link" href="/">Home</a>
          <a class="pill-link" href="/request-key">Request key</a>
          <form method="POST" action="/logout" style="margin:0;"><button class="logout-btn" type="submit">Log out</button></form>
        </div>
      </div>
      <p class="desc">Navigate usage by users, API keys, and time buckets. Metrics: cost, requests, input tokens, output tokens, and total tokens.</p>
      <div class="control-grid">
        <div>
          <label for="scope">Report mode</label>
          <select id="scope">
            <option value="all_users">All users by time period</option>
            <option value="user" selected>Each person by time period</option>
            <option value="key">Each API key by time period</option>
            <option value="user_key">Per-user key breakdown</option>
          </select>
        </div>
        <div>
          <label for="bucket">Time period</label>
          <select id="bucket">
            <option value="all">All time</option>
            <option value="year">By year</option>
            <option value="month">By month</option>
            <option value="week">By week</option>
            <option value="day" selected>By day</option>
          </select>
        </div>
        <div>
          <label for="metric">Metric</label>
          <select id="metric">
            <option value="cost" selected>Cost (USD)</option>
            <option value="requests">Request count</option>
            <option value="prompt_tokens">Input tokens</option>
            <option value="completion_tokens">Output tokens</option>
            <option value="total_tokens">Total tokens</option>
          </select>
        </div>
        <div>
          <label for="userFilter">User filter (optional)</label>
          <select id="userFilter">
            <option value="">All users</option>
          </select>
        </div>
        <div>
          <label for="start">Start (UTC)</label>
          <input type="date" id="start" />
        </div>
        <div>
          <label for="end">End (UTC)</label>
          <input type="date" id="end" />
        </div>
        <div>
          <label>&nbsp;</label>
          <button id="refresh" class="primary">Refresh report</button>
        </div>
      </div>
      <div class="range-pills">
        <button data-range="30d">Last 30d</button>
        <button data-range="90d">Last 90d</button>
        <button data-range="365d">Last 365d</button>
        <button data-range="ytd">YTD</button>
      </div>
      <div class="status" id="status"></div>
    </section>

    <section class="stats">
      <div class="stat"><div class="k">Cost</div><div class="v" id="s-cost">$0.00</div></div>
      <div class="stat"><div class="k">Requests</div><div class="v" id="s-requests">0</div></div>
      <div class="stat"><div class="k">Input tokens</div><div class="v" id="s-in">0</div></div>
      <div class="stat"><div class="k">Output tokens</div><div class="v" id="s-out">0</div></div>
      <div class="stat"><div class="k">Total tokens</div><div class="v" id="s-total">0</div></div>
    </section>

    <section class="viz-grid">
      <article class="card">
        <div class="title-row">
          <h3>Trend</h3>
          <span class="pill" id="period-pill"></span>
        </div>
        <canvas id="trendChart"></canvas>
      </article>
      <article class="card">
        <div class="title-row">
          <h3>Top totals</h3>
          <span class="pill" id="mode-pill"></span>
        </div>
        <canvas id="totalsChart"></canvas>
      </article>
      <article class="card viz-wide">
        <div class="title-row">
          <h3>Stacked by label</h3>
          <span class="pill" id="metric-pill"></span>
        </div>
        <canvas id="stackedChart"></canvas>
      </article>
      <article class="card viz-wide">
        <div class="title-row">
          <h3>Detailed rows</h3>
          <span class="pill" id="rows-pill"></span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Period</th>
                <th>Label</th>
                <th class="right">Cost</th>
                <th class="right">Requests</th>
                <th class="right">Input</th>
                <th class="right">Output</th>
                <th class="right">Total</th>
              </tr>
            </thead>
            <tbody id="rowsBody"></tbody>
          </table>
        </div>
      </article>
    </section>
  </div>

  <script>
    const COLORS = ['#22d3ee','#a855f7','#ef4444','#f59e0b','#10b981','#3b82f6','#f97316','#84cc16','#e879f9','#06b6d4','#14b8a6','#fb7185'];
    const metricMeta = {
      cost: { label: 'Cost (USD)' },
      requests: { label: 'Request count' },
      prompt_tokens: { label: 'Input tokens' },
      completion_tokens: { label: 'Output tokens' },
      total_tokens: { label: 'Total tokens' },
    };

    const controls = {
      scope: document.getElementById('scope'),
      bucket: document.getElementById('bucket'),
      metric: document.getElementById('metric'),
      userFilter: document.getElementById('userFilter'),
      start: document.getElementById('start'),
      end: document.getElementById('end'),
      refresh: document.getElementById('refresh'),
      status: document.getElementById('status'),
    };

    const summaryEls = {
      cost: document.getElementById('s-cost'),
      requests: document.getElementById('s-requests'),
      input: document.getElementById('s-in'),
      output: document.getElementById('s-out'),
      total: document.getElementById('s-total'),
    };

    const pills = {
      period: document.getElementById('period-pill'),
      mode: document.getElementById('mode-pill'),
      metric: document.getElementById('metric-pill'),
      rows: document.getElementById('rows-pill'),
    };

    const rowsBody = document.getElementById('rowsBody');
    const trendCtx = document.getElementById('trendChart').getContext('2d');
    const totalsCtx = document.getElementById('totalsChart').getContext('2d');
    const stackedCtx = document.getElementById('stackedChart').getContext('2d');
    let trendChart, totalsChart, stackedChart;

    function fmtInt(v) {
      return Number(v || 0).toLocaleString('en-US');
    }

    function fmtCurrency(v) {
      return '$' + Number(v || 0).toFixed(2);
    }

    function fmtMetric(metric, value) {
      if (metric === 'cost') return fmtCurrency(value);
      return fmtInt(value);
    }

    function todayIso() {
      return new Date().toISOString().slice(0, 10);
    }

    function setDateRangeByDays(days) {
      const end = new Date();
      const start = new Date(end.getTime() - days * 86400000);
      controls.start.value = start.toISOString().slice(0, 10);
      controls.end.value = end.toISOString().slice(0, 10);
    }

    function setYTD() {
      const now = new Date();
      controls.start.value = `${now.getUTCFullYear()}-01-01`;
      controls.end.value = todayIso();
    }

    function defaultDates() {
      setDateRangeByDays(30);
    }

    function updateDateControlState() {
      const disabled = controls.bucket.value === 'all';
      controls.start.disabled = disabled;
      controls.end.disabled = disabled;
    }

    function populateUsers(users) {
      const previous = controls.userFilter.value;
      controls.userFilter.innerHTML = '<option value="">All users</option>';
      (users || []).forEach(user => {
        const opt = document.createElement('option');
        opt.value = user;
        opt.textContent = user;
        controls.userFilter.appendChild(opt);
      });
      if (previous) controls.userFilter.value = previous;
    }

    function colorMapForLabels(labels) {
      const sorted = [...labels].sort((a, b) => a.localeCompare(b));
      const map = new Map();
      sorted.forEach((label, i) => map.set(label, COLORS[i % COLORS.length]));
      return map;
    }

    function buildAggregates(rows, metric) {
      const periods = [];
      const seenPeriods = new Set();
      const periodTotals = new Map();
      const labelTotals = new Map();
      const matrix = new Map();

      for (const row of rows) {
        const period = row.period || '—';
        const label = row.label || '—';
        const value = Number(row[metric] || 0);
        if (!seenPeriods.has(period)) {
          seenPeriods.add(period);
          periods.push(period);
        }
        periodTotals.set(period, (periodTotals.get(period) || 0) + value);
        labelTotals.set(label, (labelTotals.get(label) || 0) + value);
        if (!matrix.has(label)) matrix.set(label, new Map());
        const labelMap = matrix.get(label);
        labelMap.set(period, (labelMap.get(period) || 0) + value);
      }

      const labelsByTotal = [...labelTotals.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([label]) => label);

      return { periods, periodTotals, labelTotals, matrix, labelsByTotal };
    }

    function toStackedSeries(agg, maxSeries = 12) {
      const topLabels = agg.labelsByTotal.slice(0, maxSeries);
      const remaining = agg.labelsByTotal.slice(maxSeries);
      const series = topLabels.map(label => ({
        label,
        values: agg.periods.map(period => (agg.matrix.get(label)?.get(period)) || 0),
      }));
      if (remaining.length > 0) {
        const values = agg.periods.map(period => {
          let sum = 0;
          for (const label of remaining) sum += (agg.matrix.get(label)?.get(period)) || 0;
          return sum;
        });
        series.push({ label: `Other (${remaining.length})`, values });
      }
      return series;
    }

    function tableRowsByValue(rows, metric) {
      return [...rows].sort((a, b) => {
        const av = Number(a[metric] || 0);
        const bv = Number(b[metric] || 0);
        if (bv !== av) return bv - av;
        if ((a.period || '') !== (b.period || '')) return String(b.period || '').localeCompare(String(a.period || ''));
        return String(a.label || '').localeCompare(String(b.label || ''));
      });
    }

    function renderSummary(summary) {
      summaryEls.cost.textContent = fmtCurrency(summary.cost);
      summaryEls.requests.textContent = fmtInt(summary.requests);
      summaryEls.input.textContent = fmtInt(summary.prompt_tokens);
      summaryEls.output.textContent = fmtInt(summary.completion_tokens);
      summaryEls.total.textContent = fmtInt(summary.total_tokens);
    }

    function destroyCharts() {
      if (trendChart) trendChart.destroy();
      if (totalsChart) totalsChart.destroy();
      if (stackedChart) stackedChart.destroy();
    }

    function renderCharts(rows, metric, scope, bucket) {
      destroyCharts();
      const agg = buildAggregates(rows, metric);
      const colorMap = colorMapForLabels(agg.labelsByTotal);
      const yTick = (v) => fmtMetric(metric, v);
      const tooltipLabel = (ctx) => `${ctx.dataset.label}: ${fmtMetric(metric, ctx.parsed.y)}`;

      trendChart = new Chart(trendCtx, {
        type: 'line',
        data: {
          labels: agg.periods,
          datasets: [{
            label: metricMeta[metric].label,
            data: agg.periods.map(period => agg.periodTotals.get(period) || 0),
            borderColor: '#22d3ee',
            backgroundColor: 'rgba(34,211,238,0.15)',
            fill: true,
            tension: 0.2,
          }],
        },
        options: {
          plugins: { tooltip: { callbacks: { label: (ctx) => fmtMetric(metric, ctx.parsed.y) } } },
          scales: { y: { ticks: { callback: yTick } } },
        },
      });

      const topLabels = agg.labelsByTotal.slice(0, 16);
      totalsChart = new Chart(totalsCtx, {
        type: 'bar',
        data: {
          labels: topLabels,
          datasets: [{
            label: metricMeta[metric].label,
            data: topLabels.map(label => agg.labelTotals.get(label) || 0),
            backgroundColor: topLabels.map(label => colorMap.get(label) || COLORS[0]),
          }],
        },
        options: {
          indexAxis: 'y',
          plugins: { tooltip: { callbacks: { label: (ctx) => fmtMetric(metric, ctx.parsed.x) } } },
          scales: { x: { ticks: { callback: yTick } } },
        },
      });

      const stackedSeries = toStackedSeries(agg);
      stackedChart = new Chart(stackedCtx, {
        type: 'bar',
        data: {
          labels: agg.periods,
          datasets: stackedSeries.map((series) => ({
            label: series.label,
            data: series.values,
            backgroundColor: colorMap.get(series.label) || '#64748b',
            stack: 'stack',
          })),
        },
        options: {
          plugins: { tooltip: { callbacks: { label: tooltipLabel } } },
          scales: {
            x: { stacked: true },
            y: { stacked: true, ticks: { callback: yTick } },
          },
        },
      });

      pills.period.textContent = bucket === 'all' ? 'All time' : `Bucket: ${bucket}`;
      pills.mode.textContent = `Mode: ${scope}`;
      pills.metric.textContent = metricMeta[metric].label;
    }

    function renderTable(rows, metric) {
      rowsBody.innerHTML = '';
      const sorted = tableRowsByValue(rows, metric).slice(0, 300);
      for (const row of sorted) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${row.period || '—'}</td>
          <td class="mono">${row.label || '—'}</td>
          <td class="right">${fmtCurrency(row.cost)}</td>
          <td class="right">${fmtInt(row.requests)}</td>
          <td class="right">${fmtInt(row.prompt_tokens)}</td>
          <td class="right">${fmtInt(row.completion_tokens)}</td>
          <td class="right">${fmtInt(row.total_tokens)}</td>
        `;
        rowsBody.appendChild(tr);
      }
      pills.rows.textContent = `${rows.length} rows`;
    }

    async function loadData() {
      const qs = new URLSearchParams();
      qs.set('scope', controls.scope.value);
      qs.set('bucket', controls.bucket.value);
      if (controls.userFilter.value) qs.set('user', controls.userFilter.value);
      if (controls.bucket.value !== 'all') {
        if (controls.start.value) qs.set('start', controls.start.value);
        if (controls.end.value) qs.set('end', controls.end.value);
      }
      const res = await fetch('/reports/data?' + qs.toString());
      if (!res.ok) throw new Error(`Unable to load report (${res.status})`);
      return res.json();
    }

    async function refresh() {
      controls.status.textContent = 'Loading...';
      updateDateControlState();
      try {
        const payload = await loadData();
        populateUsers(payload.users || []);
        renderSummary(payload.summary || {});
        renderCharts(payload.rows || [], controls.metric.value, controls.scope.value, controls.bucket.value);
        renderTable(payload.rows || [], controls.metric.value);
        let rangeText = 'All time';
        if (controls.bucket.value !== 'all') {
          rangeText = `${controls.start.value || '...'} to ${controls.end.value || '...'}`;
        }
        controls.status.textContent = `${rangeText} (UTC)`;
      } catch (err) {
        controls.status.textContent = err.message || 'Error loading report.';
      }
    }

    controls.refresh.addEventListener('click', refresh);
    controls.metric.addEventListener('change', refresh);
    controls.scope.addEventListener('change', refresh);
    controls.bucket.addEventListener('change', refresh);
    controls.userFilter.addEventListener('change', refresh);
    controls.start.addEventListener('change', refresh);
    controls.end.addEventListener('change', refresh);

    document.querySelectorAll('[data-range]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.getAttribute('data-range');
        if (key === '30d') setDateRangeByDays(30);
        else if (key === '90d') setDateRangeByDays(90);
        else if (key === '365d') setDateRangeByDays(365);
        else if (key === 'ytd') setYTD();
        controls.bucket.value = 'day';
        refresh();
      });
    });

    defaultDates();
    updateDateControlState();
    refresh();
  </script>
</body>
</html>
        """
    )
