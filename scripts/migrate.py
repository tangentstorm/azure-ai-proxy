#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db_schema import apply_pending_migrations


def load_env() -> None:
    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply pending SQLite schema migrations."
    )
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", "./data/proxy.sqlite"),
        help="SQLite database path (default: env DATABASE_PATH or ./data/proxy.sqlite).",
    )
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    result = apply_pending_migrations(args.database)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
