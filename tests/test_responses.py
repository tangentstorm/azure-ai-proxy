#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv(".env.local")
    load_dotenv()


def get_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def build_url(base: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path


def upstream_config() -> tuple[str, dict[str, str], dict[str, str]]:
    base = get_env("UPSTREAM_BASE", default="https://api.openai.com")
    if not base:
        raise SystemExit("Missing UPSTREAM_BASE for upstream request.")

    path = "/responses"

    url = build_url(base, path)

    params: dict[str, str] = {}
    api_version = get_env("UPSTREAM_API_VERSION")
    if api_version:
        params["api-version"] = api_version

    key = get_env("UPSTREAM_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise SystemExit("Missing UPSTREAM_API_KEY (or OPENAI_API_KEY).")

    headers = {"Authorization": f"Bearer {key}"}

    return url, params, headers


def proxy_config() -> tuple[str, dict[str, str]]:
    base = get_env("PROXY_BASE", default="http://localhost:8000") or "http://localhost:8000"
    path = get_env("PROXY_RESPONSES_PATH", default="/v1/responses") or "/v1/responses"
    url = build_url(base, path)

    token = get_env("PROXY_API_KEY")
    if not token:
        raise SystemExit("Missing PROXY_API_KEY for proxy request.")

    headers = {"Authorization": f"Bearer {token}"}
    user = get_env("PROXY_USER")
    if user:
        headers["X-User"] = user

    return url, headers


def parse_json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {
            "status_code": response.status_code,
            "text": response.text,
        }


def main() -> int:
    load_env()

    parser = argparse.ArgumentParser(description="Send a Responses API request to upstream and proxy.")
    parser.add_argument("message", nargs="?", default="hello", help="Message to send.")
    parser.add_argument(
        "--model",
        default=get_env("RESPONSES_MODEL", "MODEL", default="gpt-5.2-codex"),
        help="Model name to use (default: gpt-5.2-codex or env RESPONSES_MODEL).",
    )
    args = parser.parse_args()

    payload = {"model": args.model, "input": args.message}

    upstream_url, upstream_params, upstream_headers = upstream_config()
    proxy_url, proxy_headers = proxy_config()

    responses_dir = Path("responses")
    responses_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0) as client:
        upstream_response = client.post(
            upstream_url,
            headers=upstream_headers,
            params=upstream_params,
            json=payload,
        )
        proxy_response = client.post(
            proxy_url,
            headers=proxy_headers,
            json=payload,
        )

    upstream_json = parse_json(upstream_response)
    proxy_json = parse_json(proxy_response)

    (responses_dir / "upstream.json").write_text(
        json.dumps(upstream_json, indent=2, sort_keys=True) + "\n"
    )
    (responses_dir / "proxy.json").write_text(
        json.dumps(proxy_json, indent=2, sort_keys=True) + "\n"
    )

    print(f"Upstream response ({upstream_response.status_code})")
    print(json.dumps(upstream_json, indent=2, sort_keys=True))
    print()
    print(f"Proxy response ({proxy_response.status_code})")
    print(json.dumps(proxy_json, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
