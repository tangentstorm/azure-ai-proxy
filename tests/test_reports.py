import sqlite3
import tempfile
import unittest
from pathlib import Path

import tracking


def create_reporting_database(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
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
                token_id INTEGER NOT NULL,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cost REAL,
                created_at INTEGER
            )
            """
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO users (id, username) VALUES (1, 'alice'), (2, 'bob')")
        conn.execute(
            """
            INSERT INTO api_keys (id, token, user_id, created_at, revoked, note)
            VALUES
                (1, 'alicekey1111', 1, 1704067200, 0, 'a1'),
                (2, 'alicekey2222', 1, 1704067200, 0, 'a2'),
                (3, 'bobkey3333', 2, 1704067200, 0, 'b1')
            """
        )
        conn.execute(
            """
            INSERT INTO usage (token_id, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at)
            VALUES
                (1, 'gpt-test', 100, 50, 150, 1.50, 1735689600),
                (2, 'gpt-test', 200, 100, 300, 2.00, 1735689600),
                (3, 'gpt-test', 300, 200, 500, 3.00, 1735689600),
                (1, 'gpt-test', 120, 60, 180, 1.20, 1735776000)
            """
        )
        conn.commit()
    finally:
        conn.close()


class ReportQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "proxy.sqlite"
        create_reporting_database(self.db_path)
        self.original_database_path = tracking.DATABASE_PATH
        tracking.DATABASE_PATH = str(self.db_path)

    def tearDown(self) -> None:
        tracking.DATABASE_PATH = self.original_database_path
        self.tempdir.cleanup()

    def test_fetch_report_rows_user_by_day(self):
        rows = tracking.fetch_report_rows(0, 32503680000, bucket="day", scope="user")
        by_key = {(row["period"], row["label"]): row for row in rows}
        self.assertAlmostEqual(by_key[("2025-01-01", "alice")]["cost"], 3.5)
        self.assertEqual(by_key[("2025-01-01", "alice")]["requests"], 2)
        self.assertAlmostEqual(by_key[("2025-01-02", "alice")]["cost"], 1.2)
        self.assertAlmostEqual(by_key[("2025-01-01", "bob")]["cost"], 3.0)

    def test_fetch_report_rows_key_scope_uses_username_and_note(self):
        rows = tracking.fetch_report_rows(0, 32503680000, bucket="day", scope="key")
        labels = {row["label"] for row in rows}
        self.assertIn("alice :: a1", labels)
        self.assertIn("alice :: a2", labels)
        self.assertIn("bob :: b1", labels)

    def test_fetch_report_rows_all_time_bucket(self):
        rows = tracking.fetch_report_rows(0, 32503680000, bucket="all", scope="all_users")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period"], "All time")
        self.assertEqual(rows[0]["label"], "All users")
        self.assertAlmostEqual(rows[0]["cost"], 7.7)
        self.assertEqual(rows[0]["requests"], 4)

    def test_fetch_report_summary_for_single_user(self):
        summary = tracking.fetch_report_summary(0, 32503680000, username="alice")
        self.assertAlmostEqual(summary["cost"], 4.7)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["prompt_tokens"], 420)
        self.assertEqual(summary["completion_tokens"], 210)
        self.assertEqual(summary["total_tokens"], 630)


if __name__ == "__main__":
    unittest.main()
