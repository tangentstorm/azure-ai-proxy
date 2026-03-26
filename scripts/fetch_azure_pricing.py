#!/usr/bin/env python3
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_MODELS = [
    "text-embedding-ada-002",
    "text-embedding-3-small",
    "text-embedding-3-large",
    "o1",
    "model-router",
    "gpt-5.4-pro",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.1-codex-max",
    "gpt-5-nano",
    "gpt-5-mini",
    "gpt-5-chat",
    "gpt-5",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
]

EXCLUDE_RE = re.compile(
    r"\b(batch|ft|train|training|hosting|realtime|audio|vision|img|image|transcribe|tts|grader|longco|pp)\b",
    re.IGNORECASE,
)
INPUT_RE = re.compile(r"\b(inp|input|inpt)\b|txt in", re.IGNORECASE)
OUTPUT_RE = re.compile(r"\b(outp|output|opt)\b|txt out", re.IGNORECASE)
CACHED_RE = re.compile(r"\b(cached|cache|cchd|cd)\b", re.IGNORECASE)


def load_env() -> None:
    load_dotenv(".env.local")
    load_dotenv(".env")
    load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Azure retail token prices by region and map them to model pricing entries "
            "usable by MODEL_PRICING_FILE."
        )
    )
    parser.add_argument("--region", default="eastus2", help="Azure ARM region (for example eastus2).")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Model/deployment alias to resolve (repeatable). Defaults to known deployed set.",
    )
    parser.add_argument(
        "--output",
        help="Write full report JSON to this path.",
    )
    parser.add_argument(
        "--emit-model-pricing-json",
        help="Write resolved pricing object ({model:{input,output}}) to this path.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Safety cap for retail API pagination.",
    )
    parser.add_argument(
        "--include-cached",
        action="store_true",
        help="Allow cached-input meters when resolving input price.",
    )
    parser.add_argument(
        "--scope",
        choices=["global", "regional", "datazone", "any"],
        default="regional",
        help="Preferred billing scope when multiple token meters exist.",
    )
    parser.add_argument(
        "--allow-ambiguous",
        action="store_true",
        help="Resolve using the top-scoring candidate even when multiple prices tie.",
    )
    return parser.parse_args()


def retail_query_url(region: str) -> str:
    filter_expr = (
        f"armRegionName eq '{region}' and "
        "(serviceName eq 'Foundry Models' or serviceName eq 'Azure OpenAI' "
        "or contains(productName,'Azure OpenAI'))"
    )
    query = urllib.parse.urlencode(
        {
            "api-version": "2023-01-01-preview",
            "$filter": filter_expr,
        }
    )
    return f"https://prices.azure.com/api/retail/prices?{query}"


def fetch_all_prices(region: str, max_pages: int) -> list[dict]:
    url = retail_query_url(region)
    items: list[dict] = []
    pages = 0
    while url:
        pages += 1
        if pages > max_pages:
            raise RuntimeError(f"Exceeded --max-pages={max_pages}")
        with urllib.request.urlopen(url, timeout=45) as resp:
            payload = json.load(resp)
        chunk = payload.get("Items") or []
        if isinstance(chunk, list):
            items.extend(chunk)
        next_link = payload.get("NextPageLink")
        url = str(next_link) if next_link else ""
    return items


def to_per_1k(price: float, unit: str) -> Optional[float]:
    unit_l = (unit or "").strip().lower()
    if unit_l == "1k":
        return float(price)
    if unit_l == "1m":
        return float(price) / 1000.0
    return None


def infer_direction(text: str) -> Optional[str]:
    if OUTPUT_RE.search(text):
        return "output"
    if INPUT_RE.search(text):
        if CACHED_RE.search(text):
            return "cached_input"
        return "input"
    return None


def model_rules(model: str) -> tuple[list[str], list[str]]:
    m = model.lower()
    rules: Dict[str, tuple[list[str], list[str]]] = {
        "text-embedding-ada-002": (
            [r"\bembedding[-\s]?ada\b"],
            [],
        ),
        "text-embedding-3-small": (
            [r"\btext[-\s]?embedding[-\s]?3[-\s]?small\b|\bembedding[-\s]?3[-\s]?small\b"],
            [],
        ),
        "text-embedding-3-large": (
            [r"\btext[-\s]?embedding[-\s]?3[-\s]?large\b|\bembedding[-\s]?3[-\s]?large\b"],
            [],
        ),
        "o1": (
            [r"\bo1(?:[-\s]|$)"],
            [r"\bmini\b", r"\bpro\b", r"\bpreview\b", r"\bft\b", r"\bgrader\b"],
        ),
        "model-router": ([r"\brouter\b"], []),
        "gpt-5.4-pro": (
            [r"\b5[.\s-]?4\b", r"\bpro\b"],
            [],
        ),
        "gpt-5.4": (
            [r"\b5[.\s-]?4\b"],
            [r"\bpro\b", r"\bmini\b", r"\bnano\b", r"\bchat\b", r"\bcodex\b"],
        ),
        "gpt-5.3-codex": (
            [r"\b5[.\s-]?3\b", r"\bcodex\b"],
            [],
        ),
        "gpt-5.2-codex": (
            [r"\b5[.\s-]?2\b", r"\bcodex\b"],
            [],
        ),
        "gpt-5.2": (
            [r"\b5[.\s-]?2\b"],
            [r"\bcodex\b", r"\bpro\b", r"\bmini\b", r"\bnano\b", r"\bchat\b"],
        ),
        "gpt-5.1-codex-max": (
            [r"\b5[.\s-]?1\b", r"\bcodex\b", r"\bmax\b"],
            [],
        ),
        "gpt-5-nano": (
            [r"\bgpt[\s-]?5\b", r"\bnano\b"],
            [],
        ),
        "gpt-5-mini": (
            [r"\bgpt[\s-]?5\b", r"\bmini\b"],
            [],
        ),
        "gpt-5-chat": (
            [r"\b5(?:[.\s-]?2)?\b", r"\bchat\b"],
            [],
        ),
        "gpt-5": (
            [r"\bgpt[\s-]?5\b"],
            [r"\bgpt[\s-]?35\b", r"\bmini\b", r"\bnano\b", r"\bchat\b", r"\bcodex\b", r"\bpro\b", r"\b5[.\s-]?[1-4]\b"],
        ),
        "gpt-4o": (
            [r"\bgpt[\s-]?4o\b|\bgpt4o\b"],
            [r"\bmini\b", r"\baudio\b", r"\brealtime\b", r"\btranscribe\b", r"\btts\b"],
        ),
        "gpt-4.1-mini": (
            [r"\bgpt[\s-]?4[.\s-]?1\b", r"\bmini\b"],
            [r"\bnano\b", r"\bft\b", r"\bhosting\b", r"\bdev\b"],
        ),
        "gpt-4.1": (
            [r"\bgpt[\s-]?4[.\s-]?1\b"],
            [r"\bmini\b", r"\bnano\b", r"\bft\b", r"\bhosting\b", r"\bdev\b"],
        ),
    }
    return rules.get(m, ([re.escape(m)], []))


@dataclass
class Candidate:
    price_per_1k: float
    direction: str
    score: int
    sku_name: str
    meter_name: str
    product_name: str
    service_name: str
    unit: str
    retail_price: float

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "score": self.score,
            "price_per_1k": self.price_per_1k,
            "service_name": self.service_name,
            "product_name": self.product_name,
            "sku_name": self.sku_name,
            "meter_name": self.meter_name,
            "unit_of_measure": self.unit,
            "retail_price": self.retail_price,
        }


def score_candidate(text: str, model: str, direction: str, scope: str) -> int:
    score = 0
    is_global = "glbl" in text or "global" in text
    is_datazone = "dzone" in text or "data zone" in text
    is_regional = "regnl" in text or "regional" in text

    if scope == "global" and is_global:
        score += 3
    if scope == "regional" and is_regional:
        score += 3
    if scope == "datazone" and is_datazone:
        score += 3
    if scope == "any":
        if is_regional or is_datazone or is_global:
            score += 1

    if "batch" in text:
        score -= 3
    if CACHED_RE.search(text):
        score -= 3
    if direction == "output":
        score += 2
    if direction == "input":
        score += 2
    if model.startswith("text-embedding"):
        score += 3
    return score


def iter_candidates(
    items: Iterable[dict],
    model: str,
    include_cached: bool,
    scope: str,
) -> list[Candidate]:
    includes, excludes = model_rules(model)
    include_res = [re.compile(expr, re.IGNORECASE) for expr in includes]
    exclude_res = [re.compile(expr, re.IGNORECASE) for expr in excludes]
    out: list[Candidate] = []
    for item in items:
        meter = str(item.get("meterName") or "")
        sku = str(item.get("skuName") or "")
        product = str(item.get("productName") or "")
        service = str(item.get("serviceName") or "")
        unit = str(item.get("unitOfMeasure") or "")
        retail_price = item.get("retailPrice")
        if not isinstance(retail_price, (int, float)):
            continue
        per_1k = to_per_1k(float(retail_price), unit)
        if per_1k is None:
            continue

        text = f"{service} {product} {sku} {meter}".lower()
        if "azure openai" not in text:
            continue
        if "token" not in text:
            continue
        if EXCLUDE_RE.search(text):
            continue
        if not include_cached and CACHED_RE.search(text):
            continue
        if not all(expr.search(text) for expr in include_res):
            continue
        if any(expr.search(text) for expr in exclude_res):
            continue

        direction = infer_direction(text)
        if model.startswith("text-embedding"):
            direction = "input"
        if direction not in {"input", "output"}:
            continue

        out.append(
            Candidate(
                price_per_1k=per_1k,
                direction=direction,
                score=score_candidate(text, model, direction, scope),
                sku_name=sku,
                meter_name=meter,
                product_name=product,
                service_name=service,
                unit=unit,
                retail_price=float(retail_price),
            )
        )
    return out


def pick_best(candidates: list[Candidate], direction: str) -> tuple[Optional[Candidate], bool]:
    subset = [c for c in candidates if c.direction == direction]
    if not subset:
        return None, False
    subset.sort(key=lambda c: (-c.score, c.price_per_1k, c.sku_name))
    best = subset[0]
    top = [c for c in subset if c.score == best.score]
    unique_prices = {round(c.price_per_1k, 12) for c in top}
    ambiguous = len(unique_prices) > 1
    return best, ambiguous


def main() -> int:
    load_env()
    args = parse_args()
    models = args.model or list(DEFAULT_MODELS)

    items = fetch_all_prices(args.region, max_pages=args.max_pages)
    report: Dict[str, Any] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "region": args.region,
        "rows_fetched": len(items),
        "models": {},
        "resolved_pricing": {},
        "unresolved": [],
    }

    for model in models:
        candidates = iter_candidates(
            items,
            model=model,
            include_cached=args.include_cached,
            scope=args.scope,
        )
        candidates.sort(key=lambda c: (-c.score, c.price_per_1k, c.sku_name))
        input_best, input_ambiguous = pick_best(candidates, "input")
        output_best, output_ambiguous = pick_best(candidates, "output")

        model_report: Dict[str, Any] = {
            "input_best": input_best.to_dict() if input_best else None,
            "output_best": output_best.to_dict() if output_best else None,
            "input_ambiguous": input_ambiguous,
            "output_ambiguous": output_ambiguous,
            "candidate_count": len(candidates),
            "top_candidates": [c.to_dict() for c in candidates[:12]],
        }
        report["models"][model] = model_report

        resolved = True
        input_price = input_best.price_per_1k if input_best else None
        output_price = output_best.price_per_1k if output_best else None
        if model.startswith("text-embedding"):
            output_price = 0.0
            if input_price is None:
                resolved = False
        else:
            if input_price is None or output_price is None:
                resolved = False
        if (input_ambiguous or output_ambiguous) and not args.allow_ambiguous:
            resolved = False

        if resolved:
            report["resolved_pricing"][model] = {
                "input": round(float(input_price), 12),
                "output": round(float(output_price), 12),
            }
        else:
            report["unresolved"].append(model)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if args.emit_model_pricing_json:
        pricing_path = Path(args.emit_model_pricing_json)
        pricing_path.parent.mkdir(parents=True, exist_ok=True)
        pricing_path.write_text(json.dumps(report["resolved_pricing"], indent=2, sort_keys=True) + "\n")

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
