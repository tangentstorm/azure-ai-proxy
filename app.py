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

from tracking import (  # noqa: E402
    create_api_key,
    fetch_active_keys_for_label,
    fetch_cost_by_day,
    fetch_cost_by_user,
    fetch_stacked_costs,
    fetch_usage_summary,
    init_db,
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


def resolve_username(request: Request) -> str:
    """
    Resolve the caller's username from headers set by an upstream auth proxy.
    Falls back to a cookie if present.
    """
    header_candidates = [
        "x-user",
        "x-forwarded-user",
        "x-remote-user",
        "remote-user",
    ]
    for h in header_candidates:
        if h in request.headers and request.headers[h].strip():
            return request.headers[h].strip()
    cookie_user = request.cookies.get("proxy_username")
    if cookie_user:
        return cookie_user
    raise HTTPException(
        status_code=401,
        detail="User identity not found in headers/cookie; login required",
    )


def require_username(request: Request) -> str:
    return resolve_username(request)

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
        resolve_username(request)
        return RedirectResponse(url=target, status_code=302)
    except HTTPException:
        pass

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
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in</h1>
    <p>No passwords yet. Enter a username to set your session cookie.</p>
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="__NEXT__" />
      <label for="username">Username</label>
      <input id="username" name="username" placeholder="e.g. codex" autocomplete="username" required />
      <button type="submit">Continue</button>
      <p class="hint">We set a cookie named <code>proxy_username</code> for local navigation.</p>
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
    return HTMLResponse(content.replace("__NEXT__", html.escape(target)))


@app.post("/login")
async def login(username: str = Form(...), next: str = Form("/")):
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    target = sanitized_next(next)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        key="proxy_username",
        value=username,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
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
    .pills { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .pill { padding:6px 12px; border-radius:999px; border:1px solid rgba(255,255,255,0.12); background:rgba(255,255,255,0.05); color:#e2e8f0; font-weight:600; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Proxy Portal</h1>
    <p>Signed in as <strong>__USERNAME__</strong>. Request keys and view cost reports for your team.</p>
    <div>
      <div style="font-weight:700; margin-bottom:6px;">Available models</div>
      <div class="pills" id="models"><span class="pill">Loading…</span></div>
    </div>
    <div class="links">
      <a href="/request-key">Request or view keys</a>
      <a href="/reports">Cost reports</a>
    </div>
  </div>
  <script>
    async function loadModels() {
      const el = document.getElementById('models');
      try {
        const res = await fetch('/models');
        if (!res.ok) throw new Error('Failed to load models');
        const data = await res.json();
        const models = data.models || [];
        if (!models.length) {
          el.innerHTML = '<span class="pill">No models returned</span>';
          return;
        }
        el.innerHTML = '';
        models.forEach((m) => {
          const span = document.createElement('span');
          span.className = 'pill';
          span.textContent = m;
          el.appendChild(span);
        });
      } catch (err) {
        el.innerHTML = '<span class="pill">Unable to load models</span>';
      }
    }
    loadModels();
  </script>
</body>
</html>
        """
    content = content.replace("__USERNAME__", html.escape(username))
    return HTMLResponse(content)


@app.post("/admin/keys")
async def issue_key(payload: Dict[str, str], x_admin_token: str = Header(...)):
    require_admin(x_admin_token)
    label = payload.get("label") or "user"
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
    start: Optional[str] = None, end: Optional[str] = None, request: Request = None
):
    """
    Aggregated cost data for charts. Date params are YYYY-MM-DD (inclusive start, inclusive end day).
    """
    if request:
        require_username(request)
    start_ts, end_ts = parse_date_range(start, end)
    by_user = fetch_cost_by_user(start_ts, end_ts)
    by_day = fetch_cost_by_day(start_ts, end_ts)
    stacked = fetch_stacked_costs(start_ts, end_ts)

    days = sorted({row["day"] for row in stacked})
    labels = sorted({row["label"] for row in stacked})
    cost_map = {(row["label"], row["day"]): row["cost"] for row in stacked}
    stacked_series = [
        {
            "label": label,
            "costs": [cost_map.get((label, day), 0) for day in days],
        }
        for label in labels
    ]

    total_day_list = [dict(day=row["day"], cost=row["cost"]) for row in by_day]
    cumulative = []
    running = 0.0
    for row in total_day_list:
        running += row["cost"]
        cumulative.append({"day": row["day"], "cost": running})

    return {
        "period": {
            "start": start_ts,
            "end": end_ts,
        },
        "cost_by_user": [dict(label=row["label"], cost=row["cost"]) for row in by_user],
        "total_by_day": total_day_list,
        "cumulative": cumulative,
        "stacked": {
            "days": days,
            "series": stacked_series,
        },
    }


@app.post("/public/keys")
async def public_issue_key(payload: Dict[str, str]):
    username = (payload.get("username") or "").strip()
    note = (payload.get("note") or "").strip() or None
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    token = create_api_key(username, note=note)
    return {"token": token, "username": username, "note": note, "masked": mask_token(token)}


@app.get("/public/keys/{username}")
async def public_list_keys(username: str):
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    rows = fetch_active_keys_for_label(username)
    return {
        "username": username,
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
  </style>
</head>
<body>
  <div class="card">
    <div class="nav">
      <a class="pill-link" href="/">Home</a>
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
  <div id="root-data" data-username="__USERNAME__"></div>
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
  <title>Cost Reports</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: radial-gradient(circle at 10% 20%, #0f172a 0, #0a1224 30%, #050a16 70%, #020610 100%);
      --panel: rgba(255, 255, 255, 0.06);
      --border: rgba(255,255,255,0.08);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #22d3ee;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Space Grotesk","Manrope","Soehne",system-ui,-apple-system,sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 24px;
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.45);
      backdrop-filter: blur(12px);
    }
    h1 { margin: 0 0 10px; letter-spacing: -0.02em; }
    p { margin: 0 0 20px; color: var(--muted); }
    label { font-weight: 600; margin-right: 8px; }
    input { padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); background: rgba(255,255,255,0.05); color: var(--text); }
    .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }
    .pill { padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); color: var(--muted); font-size: 13px; }
    a { color: var(--accent); text-decoration: none; }
  </style>
</head>
<body>
  <div class="controls">
    <h1 style="margin:0;">Cost reports</h1>
    <span class="pill"><a href="/">Home</a></span>
    <span class="pill"><a href="/request-key">Request key</a></span>
  </div>
  <p>View cost by user and over time. Adjust the date range (UTC). Charts render entirely in the browser.</p>
  <div class="controls">
    <label for="start">Start</label><input type="date" id="start" />
    <label for="end">End</label><input type="date" id="end" />
    <button id="refresh" style="padding:10px 14px; border-radius:10px; border:1px solid var(--border); background: linear-gradient(120deg, #22d3ee, #a855f7); color: #0b1225; font-weight:700; cursor:pointer;">Refresh</button>
    <span class="pill" id="period-label"></span>
  </div>
  <div class="grid">
    <div class="card">
      <h3 style="margin:0 0 6px;">Cost by user</h3>
      <canvas id="costByUser"></canvas>
    </div>
    <div class="card">
      <h3 style="margin:0 0 6px;">Daily cost + cumulative</h3>
      <canvas id="totalByDay"></canvas>
    </div>
    <div class="card" style="grid-column: 1 / -1;">
      <h3 style="margin:0 0 6px;">Stacked cost per day by user</h3>
      <canvas id="stacked"></canvas>
    </div>
  </div>
  <script>
    const costCtx = document.getElementById('costByUser').getContext('2d');
    const totalCtx = document.getElementById('totalByDay').getContext('2d');
    const stackedCtx = document.getElementById('stacked').getContext('2d');
    let costChart, totalChart, stackedChart;

    function defaultDates() {
      const today = new Date();
      const end = today.toISOString().slice(0,10);
      const startDate = new Date(today.getTime() - 30*86400000);
      const start = startDate.toISOString().slice(0,10);
      document.getElementById('start').value = start;
      document.getElementById('end').value = end;
    }

    function palette(i) {
      const colors = ['#22d3ee','#a855f7','#f97316','#f43f5e','#10b981','#3b82f6','#8b5cf6','#f59e0b'];
      return colors[i % colors.length];
    }

    function fmtCurrency(value) {
      return '$' + (value || 0).toFixed(2);
    }

    async function loadData() {
      const start = document.getElementById('start').value;
      const end = document.getElementById('end').value;
      const qs = new URLSearchParams();
      if (start) qs.append('start', start);
      if (end) qs.append('end', end);
      const res = await fetch('/reports/data?' + qs.toString());
      if (!res.ok) throw new Error('Unable to load data');
      return res.json();
    }

    function renderCostByUser(data) {
      const labels = data.cost_by_user.map(d => d.label || '—');
      const values = data.cost_by_user.map(d => d.cost || 0);
      if (costChart) costChart.destroy();
      costChart = new Chart(costCtx, {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Cost',
            data: values,
            backgroundColor: labels.map((_,i)=>palette(i)),
          }]
        },
        options: { plugins: { tooltip: { callbacks: { label: ctx => fmtCurrency(ctx.parsed.y) } } }, scales: { y: { ticks: { callback: v => '$'+v } } } }
      });
    }

    function renderTotalByDay(data) {
      const labels = data.total_by_day.map(d => d.day);
      const values = data.total_by_day.map(d => d.cost || 0);
      const cumulative = data.cumulative.map(d => d.cost || 0);
      if (totalChart) totalChart.destroy();
      totalChart = new Chart(totalCtx, {
        data: {
          labels,
          datasets: [
            {
              type: 'bar',
              label: 'Daily cost',
              data: values,
              backgroundColor: 'rgba(34,211,238,0.5)',
              borderColor: '#22d3ee',
            },
            {
              type: 'line',
              label: 'Cumulative',
              data: cumulative,
              borderColor: '#a855f7',
              backgroundColor: 'transparent',
              tension: 0.25,
              yAxisID: 'y',
            }
          ]
        },
        options: {
          scales: { y: { ticks: { callback: v => '$'+v } } },
          plugins: { tooltip: { callbacks: { label: ctx => fmtCurrency(ctx.parsed.y) } } }
        }
      });
    }

    function renderStacked(data) {
      const labels = data.stacked.days;
      const series = data.stacked.series;
      if (stackedChart) stackedChart.destroy();
      stackedChart = new Chart(stackedCtx, {
        type: 'bar',
        data: {
          labels,
          datasets: series.map((s,i)=>({
            label: s.label || '—',
            data: s.costs,
            backgroundColor: palette(i),
            stack: 'users'
          }))
        },
        options: {
          scales: {
            x: { stacked: true },
            y: { stacked: true, ticks: { callback: v => '$'+v } }
          },
          plugins: { tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${fmtCurrency(ctx.parsed.y)}` } } }
        }
      });
    }

    async function refresh() {
      document.getElementById('period-label').textContent = 'Loading…';
      try {
        const data = await loadData();
        renderCostByUser(data);
        renderTotalByDay(data);
        renderStacked(data);
        const start = document.getElementById('start').value;
        const end = document.getElementById('end').value;
        document.getElementById('period-label').textContent = `${start} → ${end}`;
      } catch (err) {
        document.getElementById('period-label').textContent = err.message || 'Error loading';
      }
    }

    document.getElementById('refresh').addEventListener('click', refresh);
    defaultDates();
    refresh();
  </script>
</body>
</html>
        """
    )
