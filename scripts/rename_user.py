import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identity import canonicalize_username


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename a user in users and preserve related api_keys/usage joins."
    )
    parser.add_argument(
        "--database",
        default="./data/proxy.sqlite",
        help="SQLite database path (default: ./data/proxy.sqlite).",
    )
    parser.add_argument("--from-label", required=True, help="Existing username.")
    parser.add_argument("--to-label", required=True, help="Replacement username.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the change. Without this flag the script is a dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from_username = canonicalize_username(args.from_label)
    to_username = canonicalize_username(args.to_label)
    if not from_username or not to_username:
        raise SystemExit("Both usernames must be non-empty after normalization.")

    db_path = Path(args.database).expanduser()
    conn = sqlite3.connect(str(db_path))
    try:
        user_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (from_username,),
        ).fetchone()
        if not user_row:
            raise SystemExit(f"User not found: {from_username}")

        destination_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (to_username,),
        ).fetchone()

        api_keys_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM api_keys
            WHERE user_id = ?
            """,
            (user_row[0],),
        ).fetchone()[0]
        usage_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            WHERE k.user_id = ?
            """,
            (user_row[0],),
        ).fetchone()[0]

        print(f"database: {db_path}")
        print(f"from_label: {from_username}")
        print(f"to_label: {to_username}")
        print(f"api_keys linked: {api_keys_count}")
        print(f"usage rows linked: {usage_count}")

        if destination_row:
            print("destination user already exists; api_keys will be reassigned and source user removed")

        if not args.apply:
            print("dry run only; rerun with --apply to update rows")
            return 0

        with conn:
            if destination_row:
                conn.execute(
                    "UPDATE api_keys SET user_id = ? WHERE user_id = ?",
                    (destination_row[0], user_row[0]),
                )
                conn.execute(
                    "DELETE FROM users WHERE id = ?",
                    (user_row[0],),
                )
            else:
                conn.execute(
                    "UPDATE users SET username = ? WHERE id = ?",
                    (to_username, user_row[0]),
                )

        print("update applied")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
