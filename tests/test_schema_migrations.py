import sqlite3
import tempfile
import unittest
from pathlib import Path

import tracking
from db_schema import apply_pending_migrations, ensure_expected_schema
from identity import canonicalize_username


def create_legacy_database(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE api_keys (
                token TEXT PRIMARY KEY,
                label TEXT,
                created_at INTEGER,
                revoked INTEGER DEFAULT 0,
                note TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT,
                label TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cost REAL,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO api_keys (token, label, created_at, revoked, note)
            VALUES
                ('tok-1', 'alice@example.com', 100, 0, 'first'),
                ('tok-2', 'alice', 200, 0, 'second'),
                ('tok-3', 'bob', 300, 1, 'revoked')
            """
        )
        conn.execute(
            """
            INSERT INTO usage (token, label, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at)
            VALUES
                ('tok-1', 'alice@example.com', 'gpt-test', 10, 20, 30, 1.5, 1000),
                ('tok-2', 'alice', 'gpt-test', 5, 15, 20, 1.0, 1001),
                ('tok-3', 'bob', 'gpt-test', 7, 8, 15, 0.8, 1002)
            """
        )
        conn.commit()
    finally:
        conn.close()


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "proxy.sqlite"
        create_legacy_database(self.db_path)
        self.original_database_path = tracking.DATABASE_PATH

    def tearDown(self) -> None:
        tracking.DATABASE_PATH = self.original_database_path
        self.tempdir.cleanup()

    def test_apply_migrations_backfills_users_and_usage(self):
        result = apply_pending_migrations(str(self.db_path))
        self.assertEqual(result["applied_versions"], [0, 1])
        self.assertEqual(result["final_version"], 1)
        self.assertTrue(result["backup_path"])
        self.assertTrue(Path(result["backup_path"]).exists())

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()[0],
                "1",
            )
            users = conn.execute("SELECT username FROM users ORDER BY username").fetchall()
            self.assertEqual([row["username"] for row in users], ["alice", "bob"])

            api_keys = conn.execute(
                """
                SELECT k.token, u.username
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                ORDER BY k.token
                """
            ).fetchall()
            self.assertEqual(
                [(row["token"], row["username"]) for row in api_keys],
                [("tok-1", "alice"), ("tok-2", "alice"), ("tok-3", "bob")],
            )

            usage = conn.execute(
                """
                SELECT u.id, k.token, usr.username
                FROM usage u
                JOIN api_keys k ON k.id = u.token_id
                JOIN users usr ON usr.id = k.user_id
                ORDER BY u.id
                """
            ).fetchall()
            self.assertEqual(
                [(row["id"], row["token"], row["username"]) for row in usage],
                [(1, "tok-1", "alice"), (2, "tok-2", "alice"), (3, "tok-3", "bob")],
            )
        finally:
            conn.close()

    def test_tracking_session_and_key_helpers_use_migrated_schema(self):
        apply_pending_migrations(str(self.db_path))
        ensure_expected_schema(str(self.db_path))
        tracking.DATABASE_PATH = str(self.db_path)

        session_token = tracking.create_session("carol@example.com")
        session = tracking.get_active_session(session_token)
        self.assertIsNotNone(session)
        self.assertEqual(session["username"], "carol")

        previous_expiry = int(session["expires_at"])
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE session_token = ?",
                (previous_expiry - 100, session_token),
            )
            conn.commit()
        finally:
            conn.close()

        refreshed_session = tracking.get_active_session(session_token)
        self.assertIsNotNone(refreshed_session)
        self.assertGreater(int(refreshed_session["expires_at"]), previous_expiry - 100)

        api_token = tracking.create_api_key("carol@example.com", note="ui")
        key_row = tracking.get_active_key(api_token)
        self.assertIsNotNone(key_row)
        self.assertEqual(key_row["username"], canonicalize_username("carol@example.com"))

        self.assertTrue(tracking.revoke_session(session_token))
        self.assertIsNone(tracking.get_active_session(session_token))


if __name__ == "__main__":
    unittest.main()
