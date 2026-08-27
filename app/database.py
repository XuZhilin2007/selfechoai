from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 3

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE CHECK (length(trim(email)) > 0),
    password_hash TEXT NOT NULL CHECK (length(trim(password_hash)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    timezone TEXT NOT NULL CHECK (length(trim(timezone)) > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    password_changed_time TEXT NOT NULL,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL
);
"""

PERSONAL_ITEMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS personal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    type TEXT NOT NULL CHECK (length(trim(type)) > 0),
    importance TEXT NOT NULL CHECK (importance IN ('high', 'medium', 'low', 'unknown')),
    urgency TEXT NOT NULL CHECK (urgency IN ('high', 'medium', 'low', 'unknown')),
    deadline TEXT,
    estimated_time INTEGER CHECK (estimated_time IS NULL OR estimated_time > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'trash')),
    next_action TEXT,
    extra_information TEXT,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    UNIQUE (id, user_id)
);
"""

ITEM_INPUTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS item_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    item_id INTEGER,
    original_text TEXT NOT NULL CHECK (length(trim(original_text)) > 0),
    input_method TEXT NOT NULL CHECK (input_method IN ('text', 'voice')),
    processing_status TEXT NOT NULL CHECK (
        processing_status IN ('pending', 'processing', 'succeeded', 'failed')
    ),
    failure_type TEXT CHECK (
        failure_type IS NULL OR failure_type IN (
            'configuration', 'network', 'api', 'invalid_output', 'internal'
        )
    ),
    failure_message TEXT,
    created_time TEXT NOT NULL,
    FOREIGN KEY (item_id, user_id)
        REFERENCES personal_items(id, user_id) ON DELETE CASCADE
);
"""

USER_SESSIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE CHECK (length(trim(token_hash)) > 0),
    csrf_token_hash TEXT NOT NULL CHECK (length(trim(csrf_token_hash)) > 0),
    created_time TEXT NOT NULL,
    last_seen_time TEXT NOT NULL,
    expires_time TEXT NOT NULL,
    revoked_time TEXT,
    user_agent TEXT
);
"""

V3_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_personal_items_user_status_updated
    ON personal_items(user_id, status, updated_time);
CREATE INDEX IF NOT EXISTS idx_item_inputs_user_status_created
    ON item_inputs(user_id, processing_status, created_time);
CREATE INDEX IF NOT EXISTS idx_item_inputs_user_item_created
    ON item_inputs(user_id, item_id, created_time);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_expires
    ON user_sessions(user_id, expires_time);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_revoked
    ON user_sessions(user_id, revoked_time);
"""

SCHEMA_V3 = "\n".join(
    (
        USERS_TABLE_SQL,
        PERSONAL_ITEMS_TABLE_SQL,
        ITEM_INPUTS_TABLE_SQL,
        USER_SESSIONS_TABLE_SQL,
        V3_INDEXES_SQL,
        f"PRAGMA user_version = {SCHEMA_VERSION};",
    )
)

REQUIRED_V3_COLUMNS = {
    "users": {
        "id",
        "email",
        "password_hash",
        "display_name",
        "timezone",
        "status",
        "password_changed_time",
        "created_time",
        "updated_time",
    },
    "personal_items": {
        "id",
        "user_id",
        "title",
        "type",
        "importance",
        "urgency",
        "deadline",
        "estimated_time",
        "status",
        "next_action",
        "extra_information",
        "created_time",
        "updated_time",
    },
    "item_inputs": {
        "id",
        "user_id",
        "item_id",
        "original_text",
        "input_method",
        "processing_status",
        "failure_type",
        "failure_message",
        "created_time",
    },
    "user_sessions": {
        "id",
        "user_id",
        "token_hash",
        "csrf_token_hash",
        "created_time",
        "last_seen_time",
        "expires_time",
        "revoked_time",
        "user_agent",
    },
}

REQUIRED_V3_INDEXES = {
    "idx_personal_items_user_status_updated",
    "idx_item_inputs_user_status_created",
    "idx_item_inputs_user_item_created",
    "idx_user_sessions_user_expires",
    "idx_user_sessions_user_revoked",
}


class DatabaseVersionError(RuntimeError):
    pass


class DatabaseSchemaError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        """Create a new v3 database or validate an existing v3 database.

        Upgrading an existing database is intentionally not performed here.
        Production upgrades must use an explicit, backup-aware migration.
        """

        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            existing_tables = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }

            if version == 0 and not existing_tables:
                connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_V3}\nCOMMIT;")
                return

            if version != SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"database schema version {version} requires an explicit "
                    f"migration to version {SCHEMA_VERSION}"
                )

            self._validate_v3_schema(connection, existing_tables)

    @staticmethod
    def _validate_v3_schema(
        connection: sqlite3.Connection, existing_tables: set[str]
    ) -> None:
        missing_tables = set(REQUIRED_V3_COLUMNS) - existing_tables
        if missing_tables:
            names = ", ".join(sorted(missing_tables))
            raise DatabaseSchemaError(f"version 3 database is missing tables: {names}")

        for table_name, required_columns in REQUIRED_V3_COLUMNS.items():
            actual_columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            missing_columns = required_columns - actual_columns
            if missing_columns:
                names = ", ".join(sorted(missing_columns))
                raise DatabaseSchemaError(
                    f"version 3 table {table_name} is missing columns: {names}"
                )

        actual_indexes = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        missing_indexes = REQUIRED_V3_INDEXES - actual_indexes
        if missing_indexes:
            names = ", ".join(sorted(missing_indexes))
            raise DatabaseSchemaError(f"version 3 database is missing indexes: {names}")

    def check_health(self) -> None:
        """Check SQLite readability through a read-only connection."""

        database_path = self.path.expanduser().resolve()
        if not database_path.is_file():
            raise DatabaseSchemaError(
                f"database file does not exist: {database_path}"
            )
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
