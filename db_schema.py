import importlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

EXPECTED_SCHEMA_VERSION = 1
MIGRATION_MODULES = [
    "migrations.m0000_meta",
    "migrations.m0001_users_sessions_and_token_ids",
]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def load_migrations() -> list[Migration]:
    loaded: list[Migration] = []
    for module_name in MIGRATION_MODULES:
        module = importlib.import_module(module_name)
        loaded.append(
            Migration(
                version=int(module.VERSION),
                name=str(module.NAME),
                apply=module.apply,
            )
        )
    return sorted(loaded, key=lambda item: item.version)


def meta_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    return row is not None


def get_schema_version(conn: sqlite3.Connection) -> int:
    if not meta_table_exists(conn):
        return -1
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )


def create_backup(db_path: Path) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.{timestamp}.bak")
    shutil.copy2(db_path, backup_path)
    return backup_path


def apply_pending_migrations(db_path: str) -> dict:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_file = path.exists()
    conn = sqlite3.connect(str(path))
    try:
        current_version = get_schema_version(conn)
        migrations = load_migrations()
        pending = [item for item in migrations if item.version > current_version]

        backup_path = None
        if pending and existing_file:
            backup_path = create_backup(path)

        applied_versions: list[int] = []
        for migration in pending:
            with conn:
                migration.apply(conn)
                set_schema_version(conn, migration.version)
            applied_versions.append(migration.version)

        final_version = get_schema_version(conn)
    finally:
        conn.close()

    return {
        "database": str(path),
        "backup_path": str(backup_path) if backup_path else None,
        "applied_versions": applied_versions,
        "final_version": final_version,
    }


def ensure_expected_schema(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        version = get_schema_version(conn)
    finally:
        conn.close()
    if version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version is {version}, expected {EXPECTED_SCHEMA_VERSION}. "
            "Run scripts/migrate.py before starting the server."
        )
    return version
