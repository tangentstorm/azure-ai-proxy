import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

from identity import canonicalize_username
from pricing import calculate_usage_cost

Row = sqlite3.Row

DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/proxy.sqlite")
SESSION_TTL_SECONDS = 7 * 24 * 3600

db_dir = os.path.dirname(DATABASE_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Schema initialization is handled by explicit migrations."""


def get_or_create_user(username: str) -> Row:
    canonical_username = canonicalize_username(username)
    if not canonical_username:
        raise ValueError("username is required")

    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username) VALUES (?)",
            (canonical_username,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, username FROM users WHERE username = ?",
            (canonical_username,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Unable to load user row for {canonical_username}")
        return row


def create_session(username: str) -> str:
    user = get_or_create_user(username)
    now = int(time.time())
    session_token = uuid.uuid4().hex
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (user_id, session_token, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user["id"], session_token, now, now + SESSION_TTL_SECONDS),
        )
        conn.commit()
    return session_token


def get_active_session(session_token: str) -> Optional[Row]:
    now = int(time.time())
    if not session_token:
        return None

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id,
                   s.user_id,
                   s.session_token,
                   s.created_at,
                   s.expires_at,
                   u.username
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = ?
              AND s.revoked_at IS NULL
              AND s.expires_at > ?
            """,
            (session_token, now),
        ).fetchone()
        if not row:
            return None

        new_expiry = now + SESSION_TTL_SECONDS
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (new_expiry, row["session_id"]),
        )
        conn.commit()
        return conn.execute(
            """
            SELECT s.id AS session_id,
                   s.user_id,
                   s.session_token,
                   s.created_at,
                   s.expires_at,
                   u.username
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (row["session_id"],),
        ).fetchone()


def revoke_session(session_token: str) -> bool:
    now = int(time.time())
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE session_token = ? AND revoked_at IS NULL
            """,
            (now, session_token),
        )
        conn.commit()
        return cur.rowcount > 0


def store_usage(
    token_id: int,
    model: str,
    usage: Dict[str, Any],
    pricing: Dict[str, Dict[str, float]],
):
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    cost = calculate_usage_cost(
        model,
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
        pricing,
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO usage (token_id, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (token_id, model, prompt, completion, total, cost, int(time.time())),
        )
        conn.commit()


def create_api_key(username: str, note: Optional[str] = None) -> str:
    user = get_or_create_user(username)
    token = uuid.uuid4().hex
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO api_keys (token, user_id, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user["id"], note, int(time.time())),
        )
        conn.commit()
    return token


def get_active_key(token: str) -> Optional[Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT k.id AS token_id,
                   k.token,
                   k.user_id,
                   u.username AS username,
                   u.username AS label
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.token = ? AND COALESCE(k.revoked, 0) = 0
            """,
            (token,),
        ).fetchone()


def revoke_key(token: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE token = ? AND COALESCE(revoked, 0) = 0",
            (token,),
        )
        conn.commit()
        return cur.rowcount > 0


def fetch_usage_summary(token: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.model,
                   SUM(u.prompt_tokens) AS prompt_tokens,
                   SUM(u.completion_tokens) AS completion_tokens,
                   SUM(u.total_tokens) AS total_tokens,
                   SUM(u.cost) AS cost
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            WHERE k.token = ?
            GROUP BY u.model
            """,
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_cost_by_user(start_ts: int, end_ts: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT usr.username AS label, COALESCE(SUM(u.cost), 0) AS cost
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            JOIN users usr ON usr.id = k.user_id
            WHERE u.created_at >= ? AND u.created_at < ?
            GROUP BY usr.username
            ORDER BY cost DESC
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_cost_by_day(start_ts: int, end_ts: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT date(datetime(u.created_at, 'unixepoch')) AS day,
                   COALESCE(SUM(u.cost), 0) AS cost
            FROM usage u
            WHERE u.created_at >= ? AND u.created_at < ?
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
            SELECT usr.username AS label,
                   date(datetime(u.created_at, 'unixepoch')) AS day,
                   COALESCE(SUM(u.cost), 0) AS cost
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            JOIN users usr ON usr.id = k.user_id
            WHERE u.created_at >= ? AND u.created_at < ?
            GROUP BY usr.username, day
            ORDER BY day
            """,
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_report_users() -> list[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT username FROM users ORDER BY username").fetchall()
    return [str(r["username"]) for r in rows]


def fetch_report_summary(start_ts: int, end_ts: int, username: Optional[str] = None) -> dict:
    clauses = ["u.created_at >= ?", "u.created_at < ?"]
    params: list[Any] = [start_ts, end_ts]
    canonical_username = canonicalize_username(username) if username else None
    if canonical_username:
        clauses.append("usr.username = ?")
        params.append(canonical_username)

    where_sql = " AND ".join(clauses)
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(u.cost), 0) AS cost,
                   COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                   COUNT(*) AS requests
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            JOIN users usr ON usr.id = k.user_id
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
    return dict(row) if row else {
        "cost": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }


def fetch_report_rows(
    start_ts: int,
    end_ts: int,
    bucket: str,
    scope: str,
    username: Optional[str] = None,
) -> list[dict]:
    period_expr_map = {
        "all": "'All time'",
        "year": "strftime('%Y', datetime(u.created_at, 'unixepoch'))",
        "month": "strftime('%Y-%m', datetime(u.created_at, 'unixepoch'))",
        "week": "strftime('%Y-W%W', datetime(u.created_at, 'unixepoch'))",
        "day": "date(datetime(u.created_at, 'unixepoch'))",
    }
    period_sort_expr_map = {
        "all": "0",
        "year": "strftime('%Y', datetime(u.created_at, 'unixepoch'))",
        "month": "strftime('%Y%m', datetime(u.created_at, 'unixepoch'))",
        "week": "strftime('%Y%W', datetime(u.created_at, 'unixepoch'))",
        "day": "strftime('%Y%m%d', datetime(u.created_at, 'unixepoch'))",
    }
    label_expr_map = {
        "all_users": "'All users'",
        "user": "usr.username",
        "key": "usr.username || ' :: ' || COALESCE(NULLIF(trim(k.note), ''), '(no note)')",
        "user_key": "usr.username || ' :: ' || COALESCE(NULLIF(trim(k.note), ''), '(no note)')",
    }

    if bucket not in period_expr_map:
        raise ValueError(f"Unsupported bucket: {bucket}")
    if scope not in label_expr_map:
        raise ValueError(f"Unsupported scope: {scope}")

    period_expr = period_expr_map[bucket]
    period_sort_expr = period_sort_expr_map[bucket]
    label_expr = label_expr_map[scope]

    clauses = ["u.created_at >= ?", "u.created_at < ?"]
    params: list[Any] = [start_ts, end_ts]
    canonical_username = canonicalize_username(username) if username else None
    if canonical_username:
        clauses.append("usr.username = ?")
        params.append(canonical_username)
    where_sql = " AND ".join(clauses)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {period_expr} AS period,
                   {period_sort_expr} AS period_sort,
                   {label_expr} AS label,
                   COALESCE(SUM(u.cost), 0) AS cost,
                   COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                   COUNT(*) AS requests
            FROM usage u
            JOIN api_keys k ON k.id = u.token_id
            JOIN users usr ON usr.id = k.user_id
            WHERE {where_sql}
            GROUP BY period, period_sort, label
            ORDER BY period_sort, label
            """,
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_active_keys_for_label(username: str) -> list[dict]:
    canonical_username = canonicalize_username(username)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT k.token, k.note, k.created_at
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE u.username = ? AND COALESCE(k.revoked, 0) = 0
            ORDER BY k.created_at DESC
            """,
            (canonical_username,),
        ).fetchall()
    return [dict(r) for r in rows]
