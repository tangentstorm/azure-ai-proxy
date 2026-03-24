import asyncio
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from pricing import MODEL_PRICING, simplify_model_name
from tracking import Row, get_active_key, store_usage

logger = logging.getLogger("proxy")

# Configuration
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "https://api.openai.com")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY")
UPSTREAM_API_VERSION = os.getenv("UPSTREAM_API_VERSION")  # Optional upstream API version

if not UPSTREAM_API_KEY:
    raise RuntimeError("UPSTREAM_API_KEY is required")

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


def normalize_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """Normalize usage payloads across chat/responses shapes."""
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")

    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")

    total = usage.get("total_tokens")
    if total is None:
        total = usage.get("tokens")

    prompt_i = int(prompt or 0)
    completion_i = int(completion or 0)
    total_i = int(total or (prompt_i + completion_i))
    return {
        "prompt_tokens": prompt_i,
        "completion_tokens": completion_i,
        "total_tokens": total_i,
    }


def _usage_score(usage: Dict[str, Any]) -> int:
    """Prefer the most complete usage dict when scanning nested payloads."""
    score = 0
    if usage.get("prompt_tokens") or usage.get("input_tokens"):
        score += 1
    if usage.get("completion_tokens") or usage.get("output_tokens"):
        score += 1
    if usage.get("total_tokens"):
        score += 2
    return score


def extract_usage_and_model(payload: Any) -> Tuple[Optional[str], Optional[Dict[str, int]]]:
    """
    Recursively scan response payloads/events for model + usage.
    Handles nested Responses API event shapes and classic chat/completions payloads.
    """
    model: Optional[str] = None
    best_usage_raw: Optional[Dict[str, Any]] = None
    best_usage_score = -1

    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            model_value = node.get("model")
            if model_value and not model:
                model = str(model_value)

            usage_value = node.get("usage")
            if isinstance(usage_value, dict):
                score = _usage_score(usage_value)
                if score > best_usage_score:
                    best_usage_raw = usage_value
                    best_usage_score = score

            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)

    if not best_usage_raw:
        return model, None
    return model, normalize_usage(best_usage_raw)


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


def build_model_mapping(models: list[str]) -> tuple[list[str], Dict[str, str]]:
    mapping: Dict[str, str] = {real: real for real in models}
    display: list[str] = []
    grouped: Dict[str, list[str]] = {}
    for real in models:
        grouped.setdefault(simplify_model_name(real), []).append(real)

    for simplified, real_models in grouped.items():
        unique_models = sorted(set(real_models))

        # Azure's /models endpoint returns model versions, but requests still
        # need the stable deployment name. Collapse a single-version family to
        # the simplified deployment alias.
        if simplified in unique_models:
            canonical = simplified
            mapping[simplified] = canonical
            for real in unique_models:
                mapping[real] = canonical
            display.append(simplified)
            continue

        if len(unique_models) == 1:
            canonical = simplified
            real = unique_models[0]
            if simplified != real:
                mapping[simplified] = canonical
                mapping[real] = canonical
                display.append(simplified)
            else:
                display.append(real)
            continue

        display.extend(unique_models)

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

    if stream_requested:
        # For streaming, the client must outlive the function return since the
        # generator is consumed later by the ASGI server.  Create it without a
        # context manager and close it in the generator's finally block.
        timeout = httpx.Timeout(60.0, connect=10.0, read=120.0, write=60.0)
        client = httpx.AsyncClient(timeout=timeout)
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
                                model_candidate, usage_candidate = extract_usage_and_model(
                                    payload
                                )
                                if model_candidate:
                                    model_seen = model_candidate
                                if usage_candidate:
                                    usage_seen = usage_candidate
                            except json.JSONDecodeError:
                                pass
                    yield (line + "\n").encode("utf-8")
            except asyncio.CancelledError:
                logger.info("[stream] client disconnected")
            except Exception as exc:
                logger.warning("[stream] upstream stream error: %s", repr(exc))
            finally:
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                if not model_seen and json_payload and json_payload.get("model"):
                    model_seen = str(json_payload.get("model"))
                if usage_seen and model_seen:
                    store_usage(
                        client_key["token"],
                        client_key["label"],
                        model_seen,
                        usage_seen,
                        MODEL_PRICING,
                    )
                elif usage_seen:
                    logger.warning("[usage] stream usage seen but model missing")

        return StreamingResponse(
            stream_iter(),
            status_code=upstream_resp.status_code,
            headers=filtered_response_headers(upstream_resp.headers),
            media_type=upstream_resp.headers.get("content-type"),
        )

    # Non-streaming request
    timeout = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
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
            model, usage = extract_usage_and_model(payload)
            if usage:
                if not model and json_payload and json_payload.get("model"):
                    model = str(json_payload.get("model"))
                if model:
                    store_usage(
                        client_key["token"],
                        client_key["label"],
                        model,
                        usage,
                        MODEL_PRICING,
                    )
                else:
                    logger.warning("[usage] non-stream usage seen but model missing")
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
