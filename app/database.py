from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_V3_VERSION = 3
SCHEMA_V4_VERSION = 4
SCHEMA_VERSION = 5

USERS_V3_TABLE_SQL = """
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

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE CHECK (length(trim(email)) > 0),
    password_hash TEXT NOT NULL CHECK (length(trim(password_hash)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    timezone TEXT NOT NULL CHECK (length(trim(timezone)) > 0),
    default_reminder_time TEXT NOT NULL DEFAULT '09:00' CHECK (
        length(default_reminder_time) = 5
        AND default_reminder_time GLOB '[0-2][0-9]:[0-5][0-9]'
        AND substr(default_reminder_time, 1, 2) BETWEEN '00' AND '23'
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    password_changed_time TEXT NOT NULL,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL
);
"""

PERSONAL_ITEMS_V3_TABLE_SQL = """
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
    reminder_prompt_dismissed_at TEXT,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    UNIQUE (id, user_id)
);
"""

ITEM_INPUTS_V4_TABLE_SQL = """
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

ITEM_INPUTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS item_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    item_id INTEGER,
    source_draft_id INTEGER CHECK (
        source_draft_id IS NULL OR source_draft_id > 0
    ),
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

USER_SESSIONS_OWNERSHIP_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_id_user
    ON user_sessions(id, user_id);
"""

REMINDERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    item_id INTEGER NOT NULL,
    source_expression TEXT CHECK (
        source_expression IS NULL OR length(trim(source_expression)) > 0
    ),
    scheduled_timezone TEXT NOT NULL CHECK (
        length(trim(scheduled_timezone)) > 0
    ),
    remind_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('needs_confirmation', 'scheduled', 'due', 'cancelled')
    ),
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    due_time TEXT,
    cancelled_time TEXT,
    cancel_reason TEXT CHECK (
        cancel_reason IS NULL OR cancel_reason IN (
            'user_cancelled', 'item_completed', 'item_trashed'
        )
    ),
    surfaced_time TEXT,
    FOREIGN KEY (item_id, user_id)
        REFERENCES personal_items(id, user_id) ON DELETE CASCADE,
    UNIQUE (id, user_id),
    CHECK (
        (status = 'needs_confirmation'
            AND remind_at IS NULL
            AND due_time IS NULL
            AND cancelled_time IS NULL
            AND cancel_reason IS NULL)
        OR (status = 'scheduled'
            AND remind_at IS NOT NULL
            AND due_time IS NULL
            AND cancelled_time IS NULL
            AND cancel_reason IS NULL)
        OR (status = 'due'
            AND remind_at IS NOT NULL
            AND due_time IS NOT NULL
            AND cancelled_time IS NULL
            AND cancel_reason IS NULL)
        OR (status = 'cancelled'
            AND due_time IS NULL
            AND cancelled_time IS NOT NULL
            AND cancel_reason IS NOT NULL)
    ),
    CHECK (surfaced_time IS NULL OR status = 'due')
);
"""

PUSH_SUBSCRIPTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    session_id INTEGER NOT NULL,
    endpoint TEXT NOT NULL CHECK (length(trim(endpoint)) > 0),
    p256dh TEXT NOT NULL CHECK (length(trim(p256dh)) > 0),
    auth TEXT NOT NULL CHECK (length(trim(auth)) > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'invalid', 'revoked')),
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    invalidated_time TEXT,
    last_error_code TEXT,
    FOREIGN KEY (session_id, user_id)
        REFERENCES user_sessions(id, user_id) ON DELETE CASCADE,
    UNIQUE (id, user_id),
    CHECK (
        (status = 'active' AND invalidated_time IS NULL)
        OR (status IN ('invalid', 'revoked') AND invalidated_time IS NOT NULL)
    )
);
"""

REMINDER_DELIVERIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reminder_id INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'sending', 'sent', 'failed', 'unknown')
    ),
    attempted_time TEXT,
    finished_time TEXT,
    provider_status TEXT,
    FOREIGN KEY (reminder_id, user_id)
        REFERENCES reminders(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (subscription_id, user_id)
        REFERENCES push_subscriptions(id, user_id) ON DELETE CASCADE
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
        USERS_V3_TABLE_SQL,
        PERSONAL_ITEMS_V3_TABLE_SQL,
        ITEM_INPUTS_V4_TABLE_SQL,
        USER_SESSIONS_TABLE_SQL,
        V3_INDEXES_SQL,
        f"PRAGMA user_version = {SCHEMA_V3_VERSION};",
    )
)

V4_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_reminders_status_remind
    ON reminders(status, remind_at, id);
CREATE INDEX IF NOT EXISTS idx_reminders_user_status_remind
    ON reminders(user_id, status, remind_at, id);
CREATE INDEX IF NOT EXISTS idx_reminders_user_item_created
    ON reminders(user_id, item_id, created_time, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reminders_user_item_active
    ON reminders(user_id, item_id)
    WHERE status IN ('needs_confirmation', 'scheduled');
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_status
    ON push_subscriptions(user_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_push_subscriptions_endpoint
    ON push_subscriptions(endpoint);
CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_status
    ON reminder_deliveries(user_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_deliveries_pair
    ON reminder_deliveries(reminder_id, subscription_id);
"""

SCHEMA_V4 = "\n".join(
    (
        USERS_TABLE_SQL,
        PERSONAL_ITEMS_TABLE_SQL,
        ITEM_INPUTS_V4_TABLE_SQL,
        USER_SESSIONS_TABLE_SQL,
        V3_INDEXES_SQL,
        USER_SESSIONS_OWNERSHIP_INDEX_SQL,
        REMINDERS_TABLE_SQL,
        PUSH_SUBSCRIPTIONS_TABLE_SQL,
        REMINDER_DELIVERIES_TABLE_SQL,
        V4_INDEXES_SQL,
        f"PRAGMA user_version = {SCHEMA_V4_VERSION};",
    )
)

CAPTURE_DRAFTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS capture_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    current_text TEXT NOT NULL DEFAULT '' CHECK (length(current_text) <= 10000),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    UNIQUE (id, user_id)
);
"""

VOICE_SEGMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS voice_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    draft_id INTEGER,
    item_input_id INTEGER,
    position INTEGER NOT NULL CHECK (position >= 0),
    client_segment_id TEXT NOT NULL CHECK (length(trim(client_segment_id)) > 0),
    storage_key TEXT NOT NULL UNIQUE CHECK (length(trim(storage_key)) > 0),
    original_size_bytes INTEGER NOT NULL CHECK (original_size_bytes > 0),
    original_sha256 TEXT NOT NULL CHECK (length(original_sha256) = 64),
    client_content_type TEXT,
    detected_container TEXT,
    detected_codec TEXT,
    sample_rate_hz INTEGER CHECK (sample_rate_hz IS NULL OR sample_rate_hz > 0),
    channels INTEGER CHECK (channels IS NULL OR channels > 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms > 0),
    transcription_status TEXT NOT NULL CHECK (
        transcription_status IN ('pending', 'transcribing', 'succeeded', 'failed')
    ),
    asr_input_kind TEXT CHECK (
        asr_input_kind IS NULL OR asr_input_kind IN ('original_direct', 'derived_wav')
    ),
    provider TEXT NOT NULL CHECK (provider = 'alibaba'),
    model TEXT NOT NULL CHECK (model = 'qwen-audio-3.0-asr-flash'),
    provider_transcript TEXT,
    provider_request_id TEXT,
    failure_code TEXT CHECK (
        failure_code IS NULL OR failure_code IN (
            'configuration', 'network', 'timeout', 'authentication',
            'quota_rate_limit', 'provider_rejected', 'provider_unavailable',
            'invalid_response', 'media_probe', 'unsupported_media',
            'conversion', 'interrupted', 'internal', 'draft_text_limit'
        )
    ),
    failure_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL,
    transcription_started_time TEXT,
    transcription_finished_time TEXT,
    FOREIGN KEY (draft_id, user_id)
        REFERENCES capture_drafts(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (item_input_id, user_id)
        REFERENCES item_inputs(id, user_id) ON DELETE CASCADE,
    CHECK ((draft_id IS NOT NULL) != (item_input_id IS NOT NULL)),
    CHECK (
        (transcription_status = 'pending'
            AND provider_transcript IS NULL
            AND failure_code IS NULL
            AND transcription_started_time IS NULL
            AND transcription_finished_time IS NULL)
        OR (transcription_status = 'transcribing'
            AND provider_transcript IS NULL
            AND failure_code IS NULL
            AND transcription_started_time IS NOT NULL
            AND transcription_finished_time IS NULL)
        OR (transcription_status = 'succeeded'
            AND provider_transcript IS NOT NULL
            AND length(trim(provider_transcript)) > 0
            AND failure_code IS NULL
            AND transcription_finished_time IS NOT NULL)
        OR (transcription_status = 'failed'
            AND failure_code IS NOT NULL
            AND failure_message IS NOT NULL
            AND transcription_finished_time IS NOT NULL)
    )
);
"""

VOICE_FILE_DELETIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS voice_file_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_key TEXT NOT NULL UNIQUE CHECK (length(trim(storage_key)) > 0),
    reason TEXT NOT NULL CHECK (
        reason IN (
            'segment_delete', 'draft_discard', 'failed_input_delete',
            'item_permanent_delete', 'orphan_cleanup'
        )
    ),
    created_time TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_time TEXT,
    last_error TEXT
);
"""

V5_PRE_VOICE_INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_inputs_id_user
    ON item_inputs(id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_item_inputs_user_source_draft
    ON item_inputs(user_id, source_draft_id)
    WHERE source_draft_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_capture_drafts_user_updated
    ON capture_drafts(user_id, updated_time);
"""

V5_INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_segments_user_client
    ON voice_segments(user_id, client_segment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_segments_draft_position
    ON voice_segments(draft_id, position)
    WHERE draft_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_segments_input_position
    ON voice_segments(item_input_id, position)
    WHERE item_input_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_segments_draft_active
    ON voice_segments(draft_id)
    WHERE draft_id IS NOT NULL
      AND transcription_status IN ('pending', 'transcribing');
CREATE INDEX IF NOT EXISTS idx_voice_segments_user_draft
    ON voice_segments(user_id, draft_id, position);
CREATE INDEX IF NOT EXISTS idx_voice_segments_user_input
    ON voice_segments(user_id, item_input_id, position);
CREATE INDEX IF NOT EXISTS idx_voice_file_deletions_created
    ON voice_file_deletions(created_time, id);
"""

SCHEMA_V5 = "\n".join(
    (
        USERS_TABLE_SQL,
        PERSONAL_ITEMS_TABLE_SQL,
        ITEM_INPUTS_TABLE_SQL,
        USER_SESSIONS_TABLE_SQL,
        V3_INDEXES_SQL,
        USER_SESSIONS_OWNERSHIP_INDEX_SQL,
        REMINDERS_TABLE_SQL,
        PUSH_SUBSCRIPTIONS_TABLE_SQL,
        REMINDER_DELIVERIES_TABLE_SQL,
        V4_INDEXES_SQL,
        CAPTURE_DRAFTS_TABLE_SQL,
        V5_PRE_VOICE_INDEXES_SQL,
        VOICE_SEGMENTS_TABLE_SQL,
        VOICE_FILE_DELETIONS_TABLE_SQL,
        V5_INDEXES_SQL,
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

REQUIRED_V4_COLUMNS = {
    "users": REQUIRED_V3_COLUMNS["users"] | {"default_reminder_time"},
    "personal_items": REQUIRED_V3_COLUMNS["personal_items"]
    | {"reminder_prompt_dismissed_at"},
    "item_inputs": REQUIRED_V3_COLUMNS["item_inputs"],
    "user_sessions": REQUIRED_V3_COLUMNS["user_sessions"],
    "reminders": {
        "id",
        "user_id",
        "item_id",
        "source_expression",
        "scheduled_timezone",
        "remind_at",
        "status",
        "created_time",
        "updated_time",
        "due_time",
        "cancelled_time",
        "cancel_reason",
        "surfaced_time",
    },
    "push_subscriptions": {
        "id",
        "user_id",
        "session_id",
        "endpoint",
        "p256dh",
        "auth",
        "status",
        "created_time",
        "updated_time",
        "invalidated_time",
        "last_error_code",
    },
    "reminder_deliveries": {
        "id",
        "user_id",
        "reminder_id",
        "subscription_id",
        "status",
        "attempted_time",
        "finished_time",
        "provider_status",
    },
}

REQUIRED_V4_INDEXES = REQUIRED_V3_INDEXES | {
    "idx_user_sessions_id_user",
    "idx_reminders_status_remind",
    "idx_reminders_user_status_remind",
    "idx_reminders_user_item_created",
    "uq_reminders_user_item_active",
    "idx_push_subscriptions_user_status",
    "uq_push_subscriptions_endpoint",
    "idx_reminder_deliveries_user_status",
    "uq_reminder_deliveries_pair",
}

REQUIRED_V5_COLUMNS = {
    **REQUIRED_V4_COLUMNS,
    "item_inputs": REQUIRED_V4_COLUMNS["item_inputs"] | {"source_draft_id"},
    "capture_drafts": {
        "id",
        "user_id",
        "current_text",
        "revision",
        "created_time",
        "updated_time",
    },
    "voice_segments": {
        "id",
        "user_id",
        "draft_id",
        "item_input_id",
        "position",
        "client_segment_id",
        "storage_key",
        "original_size_bytes",
        "original_sha256",
        "client_content_type",
        "detected_container",
        "detected_codec",
        "sample_rate_hz",
        "channels",
        "duration_ms",
        "transcription_status",
        "asr_input_kind",
        "provider",
        "model",
        "provider_transcript",
        "provider_request_id",
        "failure_code",
        "failure_message",
        "attempt_count",
        "created_time",
        "updated_time",
        "transcription_started_time",
        "transcription_finished_time",
    },
    "voice_file_deletions": {
        "id",
        "storage_key",
        "reason",
        "created_time",
        "attempt_count",
        "last_attempt_time",
        "last_error",
    },
}

REQUIRED_V5_INDEXES = REQUIRED_V4_INDEXES | {
    "idx_item_inputs_id_user",
    "uq_item_inputs_user_source_draft",
    "idx_capture_drafts_user_updated",
    "uq_voice_segments_user_client",
    "uq_voice_segments_draft_position",
    "uq_voice_segments_input_position",
    "uq_voice_segments_draft_active",
    "idx_voice_segments_user_draft",
    "idx_voice_segments_user_input",
    "idx_voice_file_deletions_created",
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
        """Create a new v5 database or validate an existing v5 database.

        Upgrading an existing database is intentionally not performed here.
        Existing installations must use an explicit, backup-aware migration.
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
                connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_V5}\nCOMMIT;")
                return

            if version != SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"database schema version {version} requires an explicit "
                    f"migration to version {SCHEMA_VERSION}"
                )

            self._validate_v5_schema(connection, existing_tables)

    @staticmethod
    def _validate_v3_schema(
        connection: sqlite3.Connection, existing_tables: set[str]
    ) -> None:
        Database._validate_schema(
            connection,
            existing_tables,
            version=SCHEMA_V3_VERSION,
            required_columns=REQUIRED_V3_COLUMNS,
            required_indexes=REQUIRED_V3_INDEXES,
        )

    @staticmethod
    def _validate_v4_schema(
        connection: sqlite3.Connection, existing_tables: set[str]
    ) -> None:
        Database._validate_schema(
            connection,
            existing_tables,
            version=SCHEMA_V4_VERSION,
            required_columns=REQUIRED_V4_COLUMNS,
            required_indexes=REQUIRED_V4_INDEXES,
        )

    @staticmethod
    def _validate_v5_schema(
        connection: sqlite3.Connection, existing_tables: set[str]
    ) -> None:
        Database._validate_schema(
            connection,
            existing_tables,
            version=SCHEMA_VERSION,
            required_columns=REQUIRED_V5_COLUMNS,
            required_indexes=REQUIRED_V5_INDEXES,
        )

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection,
        existing_tables: set[str],
        *,
        version: int,
        required_columns: dict[str, set[str]],
        required_indexes: set[str],
    ) -> None:
        missing_tables = set(required_columns) - existing_tables
        if missing_tables:
            names = ", ".join(sorted(missing_tables))
            raise DatabaseSchemaError(
                f"version {version} database is missing tables: {names}"
            )

        for table_name, table_required_columns in required_columns.items():
            actual_columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            missing_columns = table_required_columns - actual_columns
            if missing_columns:
                names = ", ".join(sorted(missing_columns))
                raise DatabaseSchemaError(
                    f"version {version} table {table_name} is missing columns: {names}"
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
        missing_indexes = required_indexes - actual_indexes
        if missing_indexes:
            names = ", ".join(sorted(missing_indexes))
            raise DatabaseSchemaError(
                f"version {version} database is missing indexes: {names}"
            )

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
