import sqlite3

from identity import canonicalize_username

VERSION = 1
NAME = "m0001_users_sessions_and_token_ids"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _normalize_existing_api_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT rowid, label FROM api_keys").fetchall()
    for rowid, label in rows:
        normalized = canonicalize_username(label)
        conn.execute(
            "UPDATE api_keys SET label = ? WHERE rowid = ?",
            (normalized, rowid),
        )


def _validate_counts(conn: sqlite3.Connection, api_keys_count: int, usage_count: int) -> None:
    new_api_keys_count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    new_usage_count = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    null_user_ids = conn.execute(
        "SELECT COUNT(*) FROM api_keys WHERE user_id IS NULL"
    ).fetchone()[0]
    null_token_ids = conn.execute(
        "SELECT COUNT(*) FROM usage WHERE token_id IS NULL"
    ).fetchone()[0]

    if new_api_keys_count != api_keys_count:
        raise RuntimeError(
            f"api_keys row count mismatch: {new_api_keys_count} != {api_keys_count}"
        )
    if new_usage_count != usage_count:
        raise RuntimeError(f"usage row count mismatch: {new_usage_count} != {usage_count}")
    if null_user_ids:
        raise RuntimeError(f"api_keys.user_id contains {null_user_ids} NULL rows")
    if null_token_ids:
        raise RuntimeError(f"usage.token_id contains {null_token_ids} NULL rows")


def apply(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "api_keys") or not _table_exists(conn, "usage"):
        raise RuntimeError("m0001 requires existing api_keys and usage tables")

    conn.execute("PRAGMA foreign_keys = OFF")
    api_keys_count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    usage_count = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    _normalize_existing_api_keys(conn)

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
        INSERT INTO users (username)
        SELECT DISTINCT label
        FROM api_keys
        WHERE COALESCE(label, '') != ''
        ORDER BY label
        """
    )

    conn.execute(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_token TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_sessions_token ON sessions (session_token)"
    )
    conn.execute(
        "CREATE INDEX idx_sessions_user_id ON sessions (user_id)"
    )

    conn.execute(
        """
        CREATE TABLE api_keys_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            created_at INTEGER,
            revoked INTEGER DEFAULT 0,
            note TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO api_keys_new (token, user_id, created_at, revoked, note)
        SELECT k.token,
               u.id,
               k.created_at,
               COALESCE(k.revoked, 0),
               k.note
        FROM api_keys k
        JOIN users u ON u.username = k.label
        ORDER BY k.created_at, k.rowid
        """
    )

    conn.execute(
        """
        CREATE TABLE usage_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id INTEGER NOT NULL,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cost REAL,
            created_at INTEGER,
            FOREIGN KEY (token_id) REFERENCES api_keys_new(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO usage_new (id, token_id, model, prompt_tokens, completion_tokens, total_tokens, cost, created_at)
        SELECT u.id,
               k.id,
               u.model,
               u.prompt_tokens,
               u.completion_tokens,
               u.total_tokens,
               u.cost,
               u.created_at
        FROM usage u
        JOIN api_keys_new k ON k.token = u.token
        ORDER BY u.id
        """
    )

    conn.execute("DROP TABLE usage")
    conn.execute("DROP TABLE api_keys")
    conn.execute("ALTER TABLE api_keys_new RENAME TO api_keys")
    conn.execute("ALTER TABLE usage_new RENAME TO usage")

    conn.execute("CREATE INDEX idx_api_keys_token ON api_keys (token)")
    conn.execute("CREATE INDEX idx_api_keys_user_id ON api_keys (user_id)")
    conn.execute("CREATE INDEX idx_usage_token_id ON usage (token_id)")

    _validate_counts(conn, api_keys_count, usage_count)
    conn.execute("PRAGMA foreign_keys = ON")
