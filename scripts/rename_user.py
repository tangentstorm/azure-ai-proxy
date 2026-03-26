import argparse
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename a user label in api_keys and usage."
    )
    parser.add_argument(
        "--database",
        default="./data/proxy.sqlite",
        help="SQLite database path (default: ./data/proxy.sqlite).",
    )
    parser.add_argument("--from-label", required=True, help="Existing user label.")
    parser.add_argument("--to-label", required=True, help="Replacement user label.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the change. Without this flag the script is a dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.database).expanduser()
    conn = sqlite3.connect(str(db_path))
    try:
        api_keys_count = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE label = ?",
            (args.from_label,),
        ).fetchone()[0]
        usage_count = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE label = ?",
            (args.from_label,),
        ).fetchone()[0]

        print(f"database: {db_path}")
        print(f"from_label: {args.from_label}")
        print(f"to_label: {args.to_label}")
        print(f"api_keys to update: {api_keys_count}")
        print(f"usage rows to update: {usage_count}")

        if not args.apply:
            print("dry run only; rerun with --apply to update rows")
            return 0

        with conn:
            conn.execute(
                "UPDATE api_keys SET label = ? WHERE label = ?",
                (args.to_label, args.from_label),
            )
            conn.execute(
                "UPDATE usage SET label = ? WHERE label = ?",
                (args.to_label, args.from_label),
            )

        print("update applied")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
