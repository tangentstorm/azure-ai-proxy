import asyncio
import json
import logging
import os
import re
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from tracking import Row, get_active_key, store_usage

logger = logging.getLogger("proxy")

# Configuration
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "https://api.openai.com")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY")
UPSTREAM_API_VERSION = os.getenv("UPSTREAM_API_VERSION")  # Optional upstream API version

DEFAULT_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Dollars per 1K tokens (input/output). Adjust as needed for your upstream.
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.0015, "output": 0.002},
}

MODEL_PRICING: Dict[str, Dict[str, float]] = DEFAULT_MODEL_PRICING
if os.getenv("MODEL_PRICING_JSON"):
    try:
        MODEL_PRICING = json.loads(os.environ["MODEL_PRICING_JSON"])
    except json.JSONDecodeError:
        raise RuntimeError("MODEL_PRICING_JSON is not valid JSON")

if not UPSTREAM_API_KEY:
    raise RuntimeError("UPSTREAM_API_KEY is required")

MODEL_SUFFIX_RE = re.compile(r"-(\\d{4}-\\d{2}-\\d{2})(?:-[a-z0-9]+)?$", re.IGNORECASE)
_models_lock = Lock()
_models_simplified: list[str] = []
_models_map: Dict[str, str] = {}
_models_loaded_at: Optional[float] = None

# In-memory debug payloads for inspection during integration.
_debug_lock = Lock()
_last_upstream_response: Optional[Dict[str, Any]] = None
_last_mapped_response: Optional[Dict[str, Any]] = None
_last_mapping_error: Optional[str] = None

router = APIRouter()


async def authenticate_client(
    authorization: Optional[str] = Header(None),
) -> Row:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization must be Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    record = get_active_key(token)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    return record


def filtered_response_headers(headers: httpx.Headers) -> Dict[str, str]:
    hop_by_hop = {
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
    return {k: v for k, v in headers.items() if k.lower() not in hop_by_hop}


def normalize_base(raw_base: str) -> str:
    """Normalize UPSTREAM_BASE by stripping query params and trailing slash."""
    return raw_base.split("?", 1)[0].rstrip("/")


def simplify_model_name(model: str) -> str:
    model = model.strip()
    match = MODEL_SUFFIX_RE.search(model)
    if match:
        return model[: match.start()]
    return model


def build_model_mapping(models: list[str]) -> tuple[list[str], Dict[str, str]]:
    simplified_map: Dict[str, str] = {}
    collisions: set[str] = set()
    for real in models:
        simplified = simplify_model_name(real)
        if simplified in simplified_map and simplified_map[simplified] != real:
            collisions.add(simplified)
        else:
            simplified_map[simplified] = real

    mapping: Dict[str, str] = {real: real for real in models}
    display: list[str] = []
    for real in models:
        simplified = simplify_model_name(real)
        if simplified in collisions:
            display.append(real)
            continue
        if simplified != real:
            mapping[simplified] = real
            display.append(simplified)
        else:
            display.append(real)

    return sorted(set(display)), mapping


def resolve_model_name(model: Optional[str]) -> Optional[str]:
    if not model:
        return model
    with _models_lock:
        return _models_map.get(model, model)


async def fetch_upstream_models() -> list[str]:
    params = {}
    if UPSTREAM_API_VERSION:
        params["api-version"] = UPSTREAM_API_VERSION

    path = "/models"
    base = normalize_base(UPSTREAM_BASE)
    url = f"{base}{path}"

    headers = {"Authorization": f"Bearer {UPSTREAM_API_KEY}"}

    timeout = httpx.Timeout(20.0, connect=5.0)
    logger.info("[models] base %s normalized %s", UPSTREAM_BASE, base)
    logger.info(
        "[models] path %s full %s params=%s header=%s",
        path,
        url,
        params,
        "Authorization",
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers, params=params)
    if resp.status_code >= 400:
        body = resp.text[:300]
        raise HTTPException(
            status_code=502,
            detail=f"Upstream {resp.status_code} error fetching models: {body}",
        )
    data = resp.json()
    models: list[str] = []
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                mid = item.get("id") or item.get("model")
                if mid:
                    models.append(str(mid))
        elif "value" in data and isinstance(data["value"], list):
            for item in data["value"]:
                mid = item.get("model") or item.get("id")
                if mid:
                    models.append(str(mid))
        elif "id" in data:
            models.append(str(data["id"]))
    if not models:
        raise HTTPException(status_code=502, detail="Upstream did not return models")
    return sorted(set(models))


async def refresh_models_cache() -> None:
    models = await fetch_upstream_models()
    display, mapping = build_model_mapping(models)
    with _models_lock:
        global _models_simplified, _models_map, _models_loaded_at
        _models_simplified = display
        _models_map = mapping
        _models_loaded_at = time.time()


async def ensure_models_loaded() -> None:
    with _models_lock:
        loaded = bool(_models_simplified)
    if not loaded:
        await refresh_models_cache()


def get_models_snapshot() -> tuple[list[str], Optional[float]]:
    with _models_lock:
        return list(_models_simplified), _models_loaded_at


def get_debug_state() -> dict:
    with _debug_lock:
        return {
            "upstream": _last_upstream_response,
            "mapped": _last_mapped_response,
            "mapping_error": _last_mapping_error,
        }


@router.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(
    full_path: str, request: Request, client_key: Row = Depends(authenticate_client)
):
    raw_body = await request.body()
    params = dict(request.query_params)

    # Detect Responses-only models and convert payload if needed
    json_payload: Optional[Dict[str, Any]] = None
    stream_requested = False
    use_responses_endpoint = full_path == "responses"
    if raw_body:
        try:
            json_payload = json.loads(raw_body)
            stream_requested = bool(json_payload.get("stream"))
            if json_payload.get("model"):
                try:
                    await ensure_models_loaded()
                    resolved_model = resolve_model_name(json_payload.get("model"))
                    if resolved_model != json_payload.get("model"):
                        json_payload["model"] = resolved_model
                        raw_body = json.dumps(json_payload).encode("utf-8")
                except Exception as exc:
                    logger.warning("[models] resolve failed: %s", repr(exc))
            logger.info(
                "[proxy] inbound method %s path %s model %s stream %s",
                request.method,
                full_path,
                json_payload.get("model"),
                stream_requested,
            )
            if stream_requested and not use_responses_endpoint:
                # Ensure we get usage back in the stream to log costs
                json_payload.setdefault("stream_options", {}).setdefault(
                    "include_usage", True
                )
                raw_body = json.dumps(json_payload).encode("utf-8")
            if json_payload:
                # Ensure content-type for json payloads
                pass
        except json.JSONDecodeError:
            json_payload = None

    base = normalize_base(UPSTREAM_BASE)
    if "api-version" not in params:
        if UPSTREAM_API_VERSION:
            params["api-version"] = UPSTREAM_API_VERSION

    # Copy incoming headers except Authorization and hop-by-hop headers
    upstream_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in {
            "authorization",
            "connection",
            "content-length",
            "host",
            "transfer-encoding",
        }
    }
    upstream_headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"

    # Choose upstream path
    path = f"/{full_path.lstrip('/')}"
    url = f"{base.rstrip('/')}{path}"
    logger.info(
        "[proxy] base %s normalized %s path %s full %s full_path %s responses %s",
        UPSTREAM_BASE,
        base,
        path,
        url,
        full_path,
        use_responses_endpoint,
    )

    if raw_body:
        upstream_headers["Content-Type"] = "application/json"
        if use_responses_endpoint and stream_requested:
            upstream_headers.setdefault("Accept", "text/event-stream")

    timeout = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream_requested:
            stream_ctx = client.stream(
                request.method,
                url,
                params=params,
                headers=upstream_headers,
                content=raw_body,
            )
            upstream_resp = await stream_ctx.__aenter__()
            usage_seen: Optional[Dict[str, Any]] = None
            model_seen: Optional[str] = None

            async def stream_iter():
                nonlocal usage_seen, model_seen
                try:
                    async for line in upstream_resp.aiter_lines():
                        if line.startswith("data:"):
                            data = line[len("data:") :].strip()
                            if data and data != "[DONE]":
                                try:
                                    payload = json.loads(data)
                                    model_seen = payload.get("model") or model_seen
                                    if payload.get("usage"):
                                        usage_seen = payload["usage"]
                                except json.JSONDecodeError:
                                    pass
                        yield (line + "\n").encode("utf-8")
                except asyncio.CancelledError:
                    logger.info("[stream] client disconnected")
                except Exception as exc:
                    logger.warning("[stream] upstream stream error: %s", repr(exc))
                finally:
                    await stream_ctx.__aexit__(None, None, None)
                    if usage_seen and model_seen:
                        store_usage(
                            client_key["token"],
                            client_key["label"],
                            model_seen,
                            usage_seen,
                            MODEL_PRICING,
                        )

            return StreamingResponse(
                stream_iter(),
                status_code=upstream_resp.status_code,
                headers=filtered_response_headers(upstream_resp.headers),
                media_type=upstream_resp.headers.get("content-type"),
            )

        # Non-streaming request
        upstream_resp = await client.request(
            request.method,
            url,
            params=params,
            headers=upstream_headers,
            content=raw_body if raw_body else None,
        )
        logger.info("[proxy] upstream status %s", upstream_resp.status_code)

    content = upstream_resp.content
    media_type = upstream_resp.headers.get("content-type")

    # Attempt to log usage from JSON responses and optionally map Responses -> chat format
    if media_type and "application/json" in media_type:
        try:
            payload = upstream_resp.json()
            log_payload("upstream_response", payload)
            log_payload(
                "upstream_output",
                payload.get("output") if isinstance(payload, dict) else None,
            )
            log_payload(
                "upstream_output_text",
                payload.get("output_text") if isinstance(payload, dict) else None,
            )
            global _last_upstream_response, _last_mapped_response, _last_mapping_error
            with _debug_lock:
                if isinstance(payload, dict):
                    _last_upstream_response = payload
                else:
                    _last_upstream_response = {"_raw": payload}
                _last_mapping_error = None
            if payload and payload.get("usage"):
                model = payload.get("model") or payload.get("data", {}).get("model")
                if model:
                    store_usage(
                        client_key["token"],
                        client_key["label"],
                        model,
                        payload["usage"],
                        MODEL_PRICING,
                    )
        except Exception as exc:
            logger.exception("[debug] response inspection failed: %s", repr(exc))
            with _debug_lock:
                _last_mapping_error = repr(exc)

    return Response(
        status_code=upstream_resp.status_code,
        content=content,
        media_type=media_type,
        headers=filtered_response_headers(upstream_resp.headers),
    )


def log_payload(label: str, data: Any):
    try:
        logger.info("[debug] %s: %s", label, json.dumps(data)[:4000])
    except Exception:
        logger.info("[debug] %s: <unserializable>", label)
