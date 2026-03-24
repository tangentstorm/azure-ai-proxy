#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pricing import calculate_usage_cost, load_model_pricing


def load_env() -> None:
    load_dotenv(".env.local")
    load_dotenv(".env")
    load_dotenv()


def parse_utc_date(value: str, *, end_of_day: bool) -> int:
    day = datetime.strptime(value, "%Y-%m-%d").date()
    if end_of_day:
        day = day + timedelta(days=1)
    dt = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@dataclass
class RowResult:
    row_id: int
    model: str
    old_cost: Optional[float]
    new_cost: Optional[float]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute usage.cost values using current pricing. "
            "Supports date ranges so you can retroactively reprice specific windows."
        )
    )
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", "./data/proxy.sqlite"),
        help="SQLite database path (default: env DATABASE_PATH or ./data/proxy.sqlite).",
    )
    parser.add_argument("--start-date", help="UTC YYYY-MM-DD inclusive filter.")
    parser.add_argument("--end-date", help="UTC YYYY-MM-DD inclusive filter.")
    parser.add_argument("--start-ts", type=int, help="Inclusive UNIX timestamp filter.")
    parser.add_argument("--end-ts", type=int, help="Exclusive UNIX timestamp filter.")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Only reprice this model (repeatable).",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Only reprice this label (repeatable).",
    )
    parser.add_argument(
        "--only-null-cost",
        action="store_true",
        help="Only target rows where cost is NULL.",
    )
    parser.add_argument(
        "--clear-unpriced",
        action="store_true",
        help="Set cost=NULL when no pricing entry exists for a targeted row.",
    )
    parser.add_argument(
        "--pricing-file",
        help="Pricing JSON file ({model:{input,output}}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of rows scanned (for testing).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist updates. Without this flag, runs as dry-run.",
    )
    return parser


def maybe_close(a: Optional[float], b: Optional[float], eps: float = 1e-12) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= eps


def main() -> int:
    load_env()
    args = build_parser().parse_args()

    if (args.start_date or args.end_date) and (args.start_ts is not None or args.end_ts is not None):
        raise SystemExit("Do not mix --start/end-date with --start/end-ts.")

    start_ts = args.start_ts
    end_ts = args.end_ts
    if args.start_date:
        start_ts = parse_utc_date(args.start_date, end_of_day=False)
    if args.end_date:
        end_ts = parse_utc_date(args.end_date, end_of_day=True)

    pricing = load_model_pricing(pricing_file=args.pricing_file)

    where = []
    params: list[Any] = []
    if start_ts is not None:
        where.append("created_at >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("created_at < ?")
        params.append(end_ts)
    if args.model:
        where.append(f"model IN ({','.join(['?'] * len(args.model))})")
        params.extend(args.model)
    if args.label:
        where.append(f"label IN ({','.join(['?'] * len(args.label))})")
        params.extend(args.label)
    if args.only_null_cost:
        where.append("cost IS NULL")

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)
    limit_sql = f" LIMIT {args.limit}" if args.limit else ""

    db_path = Path(args.database)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT id, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at
            FROM usage
            {where_sql}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()

        total_rows = len(rows)
        to_update: list[RowResult] = []
        skipped_unpriced = 0
        model_touched: Dict[str, int] = {}
        old_total = 0.0
        new_total = 0.0

        for row in rows:
            model = str(row["model"] or "")
            old_cost = row["cost"]
            usage = {
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            recomputed = calculate_usage_cost(model, usage, pricing)
            if recomputed is None and not args.clear_unpriced:
                skipped_unpriced += 1
                continue

            if not maybe_close(old_cost, recomputed):
                to_update.append(
                    RowResult(
                        row_id=int(row["id"]),
                        model=model,
                        old_cost=old_cost,
                        new_cost=recomputed,
                    )
                )
                model_touched[model] = model_touched.get(model, 0) + 1
                if old_cost is not None:
                    old_total += float(old_cost)
                if recomputed is not None:
                    new_total += float(recomputed)

        if args.apply and to_update:
            with conn:
                conn.executemany(
                    "UPDATE usage SET cost = ? WHERE id = ?",
                    [(item.new_cost, item.row_id) for item in to_update],
                )

        summary = {
            "database": str(db_path),
            "mode": "apply" if args.apply else "dry-run",
            "filters": {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "models": args.model,
                "labels": args.label,
                "only_null_cost": args.only_null_cost,
                "clear_unpriced": args.clear_unpriced,
                "limit": args.limit,
            },
            "rows_scanned": total_rows,
            "rows_to_update": len(to_update),
            "rows_skipped_unpriced": skipped_unpriced,
            "sum_old_cost_for_updates": round(old_total, 8),
            "sum_new_cost_for_updates": round(new_total, 8),
            "models_touched": model_touched,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
