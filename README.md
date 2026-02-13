# OpenAI-compatible proxy with per-user metering

Small FastAPI service that issues its own API keys, forwards requests to an upstream OpenAI-compatible server using a shared key, and records per-user token usage plus cost in SQLite.

## Setup

1) Install dependencies:
```
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```
2) Configure environment (example):
```
export UPSTREAM_API_KEY=sk-...        # required: shared key for upstream
export UPSTREAM_BASE=https://api.openai.com/v1  # optional override (include /v1 if required by upstream)
export UPSTREAM_API_VERSION=2024-02-15-preview # optional upstream API version
export ADMIN_TOKEN=supersecret        # required to create/revoke keys
export DATABASE_PATH=./data/proxy.sqlite     # optional
export PROXY_LOG_PATH=./data/proxy.log      # optional: server log file
```
Environment files: `.env.local` (preferred) and `.env` are loaded automatically.
3) Run the proxy:
```
uvicorn app:app --reload --port 8000
```
4) Sign in locally:
Visit `http://localhost:8000/login` and enter a username (no password). This sets a `proxy_username` cookie for browsing the UI.

## Managing keys

- Create a user key:
```
curl -X POST http://localhost:8000/admin/keys \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"alice","note":"prototype"}'
```
- Revoke a key:
```
curl -X POST http://localhost:8000/admin/keys/<token>/revoke \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```
- Get aggregated usage for a key:
```
curl http://localhost:8000/admin/usage/<token> \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

### Self-service UI (intranet)

- Visit `http://localhost:8000/request-key` to request keys without admin auth. Provide your username and a note; a UUID-hex key will be issued and shown once. The page also lists your existing active keys with only the first/last 4 characters visible.
- API equivalents:
```
curl -X POST http://localhost:8000/public/keys \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","note":"team tool"}'

curl http://localhost:8000/public/keys/alice
```

## Using the proxy

Send requests just like OpenAI, but with the issued token:
```
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <issued-token>" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "gpt-4o-mini",
        "messages": [{"role":"user","content":"Hello"}]
      }'
```
Streaming requests are forwarded; the proxy automatically adds `stream_options.include_usage=true` so it can log the final usage chunk. Non-streaming requests log usage when the upstream returns it.
If `UPSTREAM_API_VERSION` is set, the proxy appends `api-version=<value>` to upstream requests unless the client already provides it.
The proxy forwards `/v1/<path>` to `UPSTREAM_BASE/<path>` with no other path rewriting.

## How metering works

- Each issued token maps to a label (e.g., username). Requests authenticated with that token are proxied with the shared upstream key.
- Usage rows are recorded whenever upstream responses include `usage` (tokens and model). Costs are calculated if the model exists in `MODEL_PRICING_JSON` or the built-in defaults.
- The cost model is simple dollars per 1K tokens (input/output). Override pricing with `MODEL_PRICING_JSON` containing a JSON object like:
```
export MODEL_PRICING_JSON='{"gpt-4o":{"input":0.005,"output":0.015}}'
```

## Reports

- Visit `/reports` for an in-browser dashboard (Chart.js) with:
  - Bar chart of cost by user
  - Daily cost bar chart with cumulative cost line
  - Stacked daily cost by user
- Date range inputs (UTC) default to the last 30 days. Data comes from `/reports/data?start=YYYY-MM-DD&end=YYYY-MM-DD`.
