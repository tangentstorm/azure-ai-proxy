import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Dollars per 1K tokens (input/output). Override for your upstream as needed.
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.0015, "output": 0.002},
}

MODEL_VERSION_SUFFIX_RE = re.compile(
    r"-(\d{4}-\d{2}-\d{2})(?:-[a-z0-9]+)?$",
    re.IGNORECASE,
)


def simplify_model_name(model: str) -> str:
    model = model.strip()
    match = MODEL_VERSION_SUFFIX_RE.search(model)
    if match:
        return model[: match.start()]
    return model


def parse_pricing_payload(payload: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    parsed: Dict[str, Dict[str, float]] = {}
    for model, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"Pricing entry for {model!r} must be an object")
        entry: Dict[str, float] = {}
        for key, value in raw_entry.items():
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ValueError(f"Pricing value for {model!r}.{key} must be numeric")
            entry[key] = float(value)
        parsed[str(model)] = entry
    return parsed


def load_pricing_file(path: str) -> Dict[str, Dict[str, float]]:
    raw = Path(path).read_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MODEL_PRICING_FILE is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"MODEL_PRICING_FILE must contain a JSON object: {path}")
    return parse_pricing_payload(payload)


def merge_pricing_layers(*layers: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    merged: Dict[str, Dict[str, float]] = {}
    for layer in layers:
        for model, entry in layer.items():
            merged[model] = dict(entry)
    return merged


def load_model_pricing(
    *,
    pricing_file: Optional[str] = None,
) -> Dict[str, Dict[str, float]]:
    layers = [DEFAULT_MODEL_PRICING]

    env_pricing_file = pricing_file or os.getenv("MODEL_PRICING_FILE")

    if env_pricing_file:
        layers.append(load_pricing_file(env_pricing_file))

    return merge_pricing_layers(*layers)


def lookup_model_pricing(
    pricing: Dict[str, Dict[str, float]],
    model: str,
) -> Optional[Dict[str, float]]:
    candidates = [model]
    simplified = simplify_model_name(model)
    if simplified != model:
        candidates.append(simplified)

    for candidate in candidates:
        price = pricing.get(candidate)
        if price:
            return price
    return None


def calculate_usage_cost(
    model: str,
    usage: Dict[str, Any],
    pricing: Dict[str, Dict[str, float]],
) -> Optional[float]:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    price = lookup_model_pricing(pricing, model)
    if not price:
        return None
    return (prompt / 1000.0) * price.get("input", 0.0) + (completion / 1000.0) * price.get(
        "output",
        0.0,
    )


MODEL_PRICING: Dict[str, Dict[str, float]] = load_model_pricing()
