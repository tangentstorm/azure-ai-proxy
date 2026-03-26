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
export MODEL_PRICING_FILE=./data/model_pricing.azure.json # optional: pricing map file
```
Environment files: `.env.local` (preferred) and `.env` are loaded automatically.
3) Apply schema migrations:
```
python scripts/migrate.py --database ./data/proxy.sqlite
```
The app expects the database to already be at the current schema version and will fail fast on startup if it is not.
4) Run the proxy:
```
uvicorn app:app --reload --port 8000
```
5) Sign in locally:
Visit `http://localhost:8000/login` and enter a username (no password). This sets an opaque `proxy_session` cookie for browsing the UI. Active sessions slide forward for 7 days on each authenticated UI request, and `POST /logout` revokes the current session.

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

- Visit `http://localhost:8000/request-key` to request keys for the currently signed-in user. A UUID-hex key will be issued and shown once. The page also lists your existing active keys with only the first/last 4 characters visible.
- API equivalents:
```
curl -X POST http://localhost:8000/public/keys \
  -b "proxy_session=<session-token>" \
  -H "Content-Type: application/json" \
  -d '{"note":"team tool"}'

curl -b "proxy_session=<session-token>" \
  http://localhost:8000/public/keys/alice
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

- Each issued token belongs to a user row. Requests authenticated with that token are proxied with the shared upstream key.
- Usage rows are recorded whenever upstream responses include `usage` (tokens and model). Costs are calculated if the model exists in `MODEL_PRICING_FILE` or the built-in defaults.
- The cost model is simple dollars per 1K tokens (input/output). Override pricing with `MODEL_PRICING_FILE` pointing at a JSON object shaped like `{model:{input,output}}`.

### Azure pricing collection

Generate a pricing file from Azure retail pricing meters (region-scoped):
```
python scripts/fetch_azure_pricing.py \
  --region eastus2 \
  --output ./data/azure_pricing_report.json \
  --emit-model-pricing-json ./data/model_pricing.azure.json
```
Notes:
- The report includes `resolved_pricing` and `unresolved` models; unresolved ones need manual review.
- The emitted JSON is compatible with `MODEL_PRICING_FILE`.

### Backfill / repricing

Recompute historical `usage.cost` with date/model filters (dry-run by default):
```
python scripts/reprice_usage.py \
  --database ./data/proxy.sqlite \
  --pricing-file ./data/model_pricing.azure.json \
  --start-date 2026-03-01 \
  --end-date 2026-03-24 \
  --model gpt-5.2-codex
```

Apply changes:
```
python scripts/reprice_usage.py \
  --database ./data/proxy.sqlite \
  --pricing-file ./data/model_pricing.azure.json \
  --start-date 2026-03-01 \
  --end-date 2026-03-24 \
  --apply
```

## Reports

- Visit `/reports` for an in-browser dashboard (Chart.js) with:
  - Bar chart of cost by user
  - Daily cost bar chart with cumulative cost line
  - Stacked daily cost by user
- Date range inputs (UTC) default to the last 30 days. Data comes from `/reports/data?start=YYYY-MM-DD&end=YYYY-MM-DD`.
