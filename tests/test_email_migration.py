from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_V5,
    Database,
    DatabaseVersionError,
)
from app.migrations import v006_email_reminders
from app.migrations.v006_email_reminders import (
    EXISTING_TABLES,
    MigrationError,
    NEW_INDEXES,
    NEW_TABLES,
    inspect_v5_database,
    migrate_v5_to_v6,
)


NOW = "2026-09-04T01:00:00+00:00"


def create_populated_v5_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_V5)
    connection.execute(
        """
        INSERT INTO users (
            id, email, password_hash, display_name, timezone,
            default_reminder_time, status, password_changed_time,
            created_time, updated_time
        ) VALUES (1, 'owner@example.com', 'hash', 'Owner', 'Asia/Shanghai',
                  '09:00', 'active', ?, ?, ?)
        """,
        (NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO personal_items (
            id, user_id, title, type, importance, urgency, status,
            created_time, updated_time
        ) VALUES (1, 1, 'Item', 'task', 'medium', 'medium', 'active', ?, ?)
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO item_inputs (
            id, user_id, item_id, original_text, input_method,
            processing_status, created_time
        ) VALUES (1, 1, 1, 'Original input', 'text', 'succeeded', ?)
        """,
        (NOW,),
    )
    connection.execute(
        """
        INSERT INTO user_sessions (
            id, user_id, token_hash, csrf_token_hash, created_time,
            last_seen_time, expires_time
        ) VALUES (1, 1, 'token', 'csrf', ?, ?, '2027-09-04T01:00:00+00:00')
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO reminders (
            id, user_id, item_id, scheduled_timezone, remind_at, status,
            created_time, updated_time, due_time
        ) VALUES (1, 1, 1, 'Asia/Shanghai', ?, 'due', ?, ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO push_subscriptions (
            id, user_id, session_id, endpoint, p256dh, auth, status,
            created_time, updated_time
        ) VALUES (1, 1, 1, 'https://push.example/1', 'key', 'auth', 'active', ?, ?)
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO reminder_deliveries (
            id, user_id, reminder_id, subscription_id, status,
            attempted_time, finished_time, provider_status
        ) VALUES (1, 1, 1, 1, 'sent', ?, ?, '201')
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO capture_drafts (
            id, user_id, current_text, revision, created_time, updated_time
        ) VALUES (1, 1, 'Draft', 1, ?, ?)
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO voice_segments (
            id, user_id, item_input_id, position, client_segment_id,
            storage_key, original_size_bytes, original_sha256,
            transcription_status, provider, model, provider_transcript,
            attempt_count, created_time, updated_time,
            transcription_finished_time
        ) VALUES (
            1, 1, 1, 0, 'segment-1', 'voice/segment-1', 10, ?,
            'succeeded', 'alibaba', 'qwen-audio-3.0-asr-flash',
            'Transcript', 1, ?, ?, ?
        )
        """,
        ("0" * 64, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO voice_file_deletions (
            id, storage_key, reason, created_time
        ) VALUES (1, 'voice/orphan', 'orphan_cleanup', ?)
        """,
        (NOW,),
    )
    connection.commit()
    connection.close()


def row_counts(path: Path, tables=EXISTING_TABLES) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    finally:
        connection.close()


def test_fresh_database_initializes_directly_to_v6(tmp_path):
    path = tmp_path / "fresh-v6.db"
    Database(path).initialize()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert {
        "email_reminder_settings",
        "email_verification_challenges",
        "reminder_email_deliveries",
    } <= tables


def test_v5_to_v6_preserves_all_existing_rows_and_push_schema(tmp_path):
    path = tmp_path / "populated-v5.db"
    create_populated_v5_database(path)
    before = row_counts(path)
    connection = sqlite3.connect(path)
    original_push_delivery_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reminder_deliveries'"
    ).fetchone()[0]
    connection.close()

    result = migrate_v5_to_v6(path)

    assert result.row_counts == before
    assert row_counts(path) == before
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        migrated_push_delivery_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reminder_deliveries'"
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert migrated_push_delivery_sql == original_push_delivery_sql


def test_migration_creates_empty_constrained_email_schema_without_login_backfill(
    tmp_path,
):
    path = tmp_path / "email-schema.db"
    create_populated_v5_database(path)
    migrate_v5_to_v6(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in NEW_TABLES
        } == {table: 0 for table in NEW_TABLES}
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert set(NEW_INDEXES) <= indexes
        foreign_targets = {
            table: {
                row[2]
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            for table in NEW_TABLES
        }
        assert foreign_targets["email_reminder_settings"] == {"users"}
        assert foreign_targets["email_verification_challenges"] == {"users"}
        assert foreign_targets["reminder_email_deliveries"] == {
            "users",
            "reminders",
        }
        assert connection.execute(
            "SELECT 1 FROM email_reminder_settings WHERE user_id = 1"
        ).fetchone() is None

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO email_reminder_settings (
                    user_id, verification_status, verified_at,
                    created_time, updated_time
                ) VALUES (1, 'verified', NULL, ?, ?)
                """,
                (NOW, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO email_verification_challenges (
                    user_id, email_address, code_hmac, send_status,
                    expires_at, created_time, last_sent_at
                ) VALUES (999, 'nobody@example.com', ?, 'pending', ?, ?, ?)
                """,
                ("0" * 64, NOW, NOW, NOW),
            )
    finally:
        connection.close()


def test_check_only_is_read_only_byte_for_byte(tmp_path):
    path = tmp_path / "check-only.db"
    create_populated_v5_database(path)
    before = path.read_bytes()
    preflight = inspect_v5_database(path)
    after = path.read_bytes()
    assert preflight.schema_version == 5
    assert before == after


def test_cli_check_only_is_read_only_and_never_prompts(tmp_path):
    path = tmp_path / "cli-check-only.db"
    create_populated_v5_database(path)
    before = path.read_bytes()
    output = []
    result = v006_email_reminders.main(
        ["--database", str(path), "--check-only"],
        input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        output_fn=output.append,
    )
    assert result == 0
    assert path.read_bytes() == before
    assert "Preflight passed. No database changes were made." in output


def test_preflight_rejects_foreign_key_and_integrity_failures(
    tmp_path,
    monkeypatch,
):
    foreign_key_path = tmp_path / "foreign-key-failure.db"
    create_populated_v5_database(foreign_key_path)
    connection = sqlite3.connect(foreign_key_path)
    connection.execute(
        """
        INSERT INTO user_sessions (
            id, user_id, token_hash, csrf_token_hash, created_time,
            last_seen_time, expires_time
        ) VALUES (2, 999, 'orphan-token', 'orphan-csrf', ?, ?, ?)
        """,
        (NOW, NOW, "2027-09-04T01:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="foreign key"):
        inspect_v5_database(foreign_key_path)

    integrity_path = tmp_path / "integrity-failure.db"
    create_populated_v5_database(integrity_path)
    real_connect = sqlite3.connect

    class IntegrityFailureConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        @property
        def row_factory(self):
            return self.wrapped.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.wrapped.row_factory = value

        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                return type(
                    "IntegrityCursor",
                    (),
                    {"fetchone": lambda self: ("synthetic corruption",)},
                )()
            return self.wrapped.execute(sql, *args)

        def close(self):
            self.wrapped.close()

    def connect_with_integrity_failure(*args, **kwargs):
        return IntegrityFailureConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(
        v006_email_reminders.sqlite3,
        "connect",
        connect_with_integrity_failure,
    )
    with pytest.raises(MigrationError, match="integrity check failed"):
        inspect_v5_database(integrity_path)


def test_source_version_mismatch_and_unexpected_v6_object_are_rejected(tmp_path):
    path = tmp_path / "wrong.db"
    create_populated_v5_database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="expected database schema version 5"):
        inspect_v5_database(path)

    path = tmp_path / "unexpected.db"
    create_populated_v5_database(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE email_reminder_settings (user_id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="contains v6 tables"):
        inspect_v5_database(path)

    path = tmp_path / "unexpected-index.db"
    create_populated_v5_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE INDEX idx_email_deliveries_send_ready ON users(email)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="contains v6 indexes"):
        inspect_v5_database(path)


def test_migration_refuses_current_v6_and_never_downgrades(tmp_path):
    path = tmp_path / "already-v6.db"
    Database(path).initialize()
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="expected database schema version 5"):
        migrate_v5_to_v6(path)
    assert path.read_bytes() == before


def test_migration_rolls_back_if_index_creation_fails(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    create_populated_v5_database(path)

    def fail_indexes(_connection, _sql):
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(v006_email_reminders, "_execute_statements", fail_indexes)
    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v5_to_v6(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert "email_reminder_settings" not in tables


def test_v6_application_rejects_unmigrated_v5_database(tmp_path):
    path = tmp_path / "unmigrated.db"
    create_populated_v5_database(path)
    with pytest.raises(DatabaseVersionError, match="migration to version 6"):
        Database(path).initialize()


def test_cli_requires_exact_backup_acknowledgement(tmp_path):
    path = tmp_path / "cli.db"
    create_populated_v5_database(path)
    output = []
    result = v006_email_reminders.main(
        ["--database", str(path)],
        input_fn=lambda _prompt: "no",
        output_fn=output.append,
    )
    assert result == 2
    assert row_counts(path) == {table: 1 for table in EXISTING_TABLES}
    assert any("validated, recoverable schema v5 backup" in line for line in output)


def test_cli_accepts_only_exact_public_confirmation_phrase(tmp_path):
    path = tmp_path / "cli-confirmed.db"
    create_populated_v5_database(path)
    output = []
    result = v006_email_reminders.main(
        ["--database", str(path)],
        input_fn=lambda prompt: (
            "MIGRATE PUBLIC V5 TO V6"
            if "MIGRATE PUBLIC V5 TO V6" in prompt
            else pytest.fail("unexpected confirmation prompt")
        ),
        output_fn=output.append,
    )
    assert result == 0
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()
    assert "Migration completed successfully." in output


def test_current_schema_version_is_six_without_changing_v005_compatibility():
    assert CURRENT_SCHEMA_VERSION == 6
