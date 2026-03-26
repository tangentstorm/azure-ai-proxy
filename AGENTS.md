# Repository Guidelines

## Project Structure & Module Organization
`app.py` is the FastAPI entrypoint and mounts the proxy/router plus the login, admin, and report pages. Core request forwarding lives in `proxy.py`; usage persistence and reporting queries live in `tracking.py`; model pricing logic is in `pricing.py`; LDAP auth support is isolated in `ldap_plugin.py`. Utility scripts belong in `scripts/`, notably `fetch_azure_pricing.py` and `reprice_usage.py`. Runtime state is stored under `data/` (`proxy.sqlite`, pricing JSON, logs). Keep automated tests in `tests/` as `test_*.py`.

## Build, Test, and Development Commands
Create a local environment and install dependencies with `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`. Run the service with `uvicorn app:app --reload --port 8000`. Execute unit tests with `python -m unittest -v`. Use `python tests/test_responses.py "hello"` for a manual upstream-vs-proxy comparison; it writes snapshots to `responses/`. Reprice historical usage with `python scripts/reprice_usage.py --database ./data/proxy.sqlite --pricing-file ./data/model_pricing.azure.json --apply`.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, `snake_case` for functions and variables, `UPPER_SNAKE_CASE` for env-driven constants, and explicit type hints where practical. Keep modules focused by responsibility rather than adding large helper grab-bags. Preserve the current import style and short inline comments only where behavior is not obvious. No formatter or linter config is committed here, so match the surrounding code and keep lines readable.

## Testing Guidelines
This repo currently uses `unittest`. Name test files `tests/test_*.py`, test classes `*Tests`, and methods `test_*`. Add focused unit coverage for model mapping, proxy request shaping, pricing calculations, and SQLite usage recording when touching those areas. For behavior that depends on live upstream credentials, prefer small reproducible scripts over brittle automated integration tests.

## Commit & Pull Request Guidelines
Recent history favors short, imperative commit subjects such as `record usage even when streaming` or scoped variants like `ldap: attempt to reconnect/recover`. Keep subjects concise and behavior-focused. PRs should state the user-visible change, list any required env vars or migration steps, and include sample requests or screenshots when UI pages (`/login`, `/request-key`, `/reports`) change.

## Security & Configuration Tips
Do not commit real secrets in `.env.local`, logs, or `data/` artifacts. Validate changes against `ADMIN_TOKEN`, upstream API settings, and `MODEL_PRICING_FILE` expectations before merging. Treat SQLite files and generated pricing reports as local runtime data, not source of truth.
