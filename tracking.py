import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

Row = sqlite3.Row

DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/proxy.sqlite")

db_dir = os.path.dirname(DATABASE_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
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
            CREATE TABLE IF NOT EXISTS usage (
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_token ON usage (token)")
        # Add note column if missing (for existing databases)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "note" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN note TEXT")
        conn.commit()


def store_usage(
    token: str,
    label: str,
    model: str,
    usage: Dict[str, Any],
    pricing: Dict[str, Dict[str, float]],
):
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    price = pricing.get(model, {})
    cost = None
    if price:
        cost = (prompt / 1000) * price.get("input", 0) + (completion / 1000) * price.get(
            "output", 0
        )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO usage (token, label, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, label, model, prompt, completion, total, cost, int(time.time())),
        )
        conn.commit()


def create_api_key(label: str, note: Optional[str] = None) -> str:
    token = uuid.uuid4().hex
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (token, label, note, created_at) VALUES (?, ?, ?, ?)",
            (token, label, note, int(time.time())),
        )
        conn.commit()
    return token


def get_active_key(token: str) -> Optional[Row]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT token, label FROM api_keys WHERE token = ? AND revoked = 0", (token,)
        ).fetchone()
        return row


def revoke_key(token: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE token = ? AND revoked = 0", (token,)
        )
        conn.commit()
        return cur.rowcount > 0


def fetch_usage_summary(token: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(cost) AS cost
            FROM usage WHERE token = ?
            GROUP BY model
            """,
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_cost_by_user(start_ts: int, end_ts: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT label, COALESCE(SUM(cost), 0) as cost
            FROM usage
            WHERE created_at >= ? AND created_at < ?
            GROUP BY label
            ORDER BY cost DESC
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_cost_by_day(start_ts: int, end_ts: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT date(datetime(created_at, 'unixepoch')) as day,
                   COALESCE(SUM(cost), 0) as cost
            FROM usage
            WHERE created_at >= ? AND created_at < ?
            GROUP BY day
            ORDER BY day
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_stacked_costs(start_ts: int, end_ts: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT label,
                   date(datetime(created_at, 'unixepoch')) as day,
                   COALESCE(SUM(cost), 0) as cost
            FROM usage
            WHERE created_at >= ? AND created_at < ?
            GROUP BY label, day
            ORDER BY day
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_active_keys_for_label(username: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT token, note, created_at FROM api_keys WHERE label = ? AND revoked = 0 ORDER BY created_at DESC",
            (username,),
        ).fetchall()
    return [dict(r) for r in rows]
