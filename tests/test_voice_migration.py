from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import (
    CAPTURE_DRAFTS_TABLE_SQL,
    Database,
    DatabaseVersionError,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_VERSION,
)
from app.migrations import v005_voice_capture
from app.migrations.v005_voice_capture import (
    MigrationError,
    inspect_v4_database,
    main,
    migrate_v4_to_v5,
)


NOW = "2026-09-07T01:00:00+00:00"


def create_v4_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_V4)
    connection.execute(
        """
        INSERT INTO users (
            id, email, password_hash, display_name, timezone,
            default_reminder_time, status, password_changed_time,
            created_time, updated_time
        ) VALUES (7, ?, ?, ?, 'Asia/Shanghai', '08:30', 'active', ?, ?, ?)
        """,
        ("owner@example.com", "preserved-hash", "Owner", NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO personal_items (
            id, user_id, title, type, importance, urgency, deadline,
            estimated_time, status, next_action, extra_information,
            reminder_prompt_dismissed_at, created_time, updated_time
        ) VALUES (
            11, 7, ?, 'note', 'unknown', 'unknown', NULL, NULL,
            'active', NULL, ?, NULL, ?, ?
        )
        """,
        ("必须保留的事项", '{"context":"不能丢失"}', NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO item_inputs (
            id, user_id, item_id, original_text, input_method,
            processing_status, failure_type, failure_message, created_time
        ) VALUES (13, 7, 11, ?, 'text', 'succeeded', NULL, NULL, ?)
        """,
        ("必须逐字保留的原始输入", NOW),
    )
    connection.execute(
        """
        INSERT INTO user_sessions (
            id, user_id, token_hash, csrf_token_hash, created_time,
            last_seen_time, expires_time, revoked_time, user_agent
        ) VALUES (17, 7, 'session-hash', 'csrf-hash', ?, ?, ?, NULL, 'test')
        """,
        (NOW, NOW, "2027-09-07T01:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO reminders (
            id, user_id, item_id, source_expression, scheduled_timezone,
            remind_at, status, created_time, updated_time, due_time,
            cancelled_time, cancel_reason, surfaced_time
        ) VALUES (
            19, 7, 11, '明天', 'Asia/Shanghai', NULL,
            'needs_confirmation', ?, ?, NULL, NULL, NULL, NULL
        )
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO push_subscriptions (
            id, user_id, session_id, endpoint, p256dh, auth, status,
            created_time, updated_time, invalidated_time, last_error_code
        ) VALUES (
            23, 7, 17, 'https://push.invalid/endpoint', 'p256dh', 'auth',
            'active', ?, ?, NULL, NULL
        )
        """,
        (NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO reminder_deliveries (
            id, user_id, reminder_id, subscription_id, status,
            attempted_time, finished_time, provider_status
        ) VALUES (29, 7, 19, 23, 'queued', NULL, NULL, NULL)
        """
    )
    connection.commit()
    connection.close()


def logical_snapshot(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_public_v4_migrates_to_v5_and_preserves_all_community_data(
    tmp_path: Path,
):
    database_path = tmp_path / "community-v4.db"
    create_v4_database(database_path)

    preflight = inspect_v4_database(database_path)
    result = migrate_v4_to_v5(database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    user = connection.execute("SELECT * FROM users WHERE id = 7").fetchone()
    item = connection.execute("SELECT * FROM personal_items WHERE id = 11").fetchone()
    item_input = connection.execute(
        "SELECT * FROM item_inputs WHERE id = 13"
    ).fetchone()
    session = connection.execute(
        "SELECT * FROM user_sessions WHERE id = 17"
    ).fetchone()
    reminder = connection.execute("SELECT * FROM reminders WHERE id = 19").fetchone()
    subscription = connection.execute(
        "SELECT * FROM push_subscriptions WHERE id = 23"
    ).fetchone()
    delivery = connection.execute(
        "SELECT * FROM reminder_deliveries WHERE id = 29"
    ).fetchone()
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    empty_counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("capture_drafts", "voice_segments", "voice_file_deletions")
    }
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()

    assert preflight.schema_version == 4
    assert preflight.user_count == result.user_count == 1
    assert preflight.personal_item_count == result.personal_item_count == 1
    assert preflight.item_input_count == result.item_input_count == 1
    assert preflight.user_session_count == result.user_session_count == 1
    assert preflight.reminder_count == result.reminder_count == 1
    assert preflight.push_subscription_count == result.push_subscription_count == 1
    assert preflight.reminder_delivery_count == result.reminder_delivery_count == 1
    assert version == SCHEMA_VERSION
    assert user["email"] == "owner@example.com"
    assert item["title"] == "必须保留的事项"
    assert item["extra_information"] == '{"context":"不能丢失"}'
    assert item_input["original_text"] == "必须逐字保留的原始输入"
    assert item_input["source_draft_id"] is None
    assert session["token_hash"] == "session-hash"
    assert reminder["status"] == "needs_confirmation"
    assert subscription["endpoint"] == "https://push.invalid/endpoint"
    assert delivery["status"] == "queued"
    assert {"capture_drafts", "voice_segments", "voice_file_deletions"} <= tables
    assert {
        "uq_voice_segments_user_client",
        "uq_voice_segments_draft_active",
        "uq_item_inputs_user_source_draft",
    } <= indexes
    assert empty_counts == {
        "capture_drafts": 0,
        "voice_segments": 0,
        "voice_file_deletions": 0,
    }
    assert foreign_key_errors == []
    assert integrity == "ok"
    with pytest.raises(DatabaseVersionError, match="migration to version 6"):
        Database(database_path).initialize()


def test_v5_migration_check_only_is_byte_for_byte_read_only(tmp_path: Path):
    database_path = tmp_path / "check-only-v4.db"
    create_v4_database(database_path)
    before = database_path.read_bytes()
    output: list[str] = []

    exit_code = main(
        ["--database", str(database_path), "--check-only"],
        input_fn=lambda prompt: pytest.fail("check-only must not prompt"),
        output_fn=output.append,
    )

    assert exit_code == 0
    assert database_path.read_bytes() == before
    assert "Preflight passed. No database changes were made." in output


def test_v5_migration_requires_exact_confirmation(tmp_path: Path):
    database_path = tmp_path / "confirmation-v4.db"
    create_v4_database(database_path)
    before = database_path.read_bytes()
    prompts: list[str] = []

    exit_code = main(
        ["--database", str(database_path)],
        input_fn=lambda prompt: prompts.append(prompt) or "no",
        output_fn=lambda message: None,
    )

    assert exit_code == 2
    assert prompts == ['Type "MIGRATE V4 TO V5" to execute the transaction: ']
    assert database_path.read_bytes() == before


def test_v5_migration_rolls_back_every_change_on_post_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_path = tmp_path / "rollback-v4.db"
    create_v4_database(database_path)
    before = logical_snapshot(database_path)

    def fail_validation(*args, **kwargs):
        raise RuntimeError("injected post-validation failure")

    monkeypatch.setattr(v005_voice_capture, "_validate_migrated_data", fail_validation)

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v4_to_v5(database_path)

    assert logical_snapshot(database_path) == before
    assert inspect_v4_database(database_path).schema_version == 4


def test_v5_migration_rejects_wrong_source_and_repeated_invocation(tmp_path: Path):
    v3_path = tmp_path / "v3.db"
    connection = sqlite3.connect(v3_path)
    connection.executescript(SCHEMA_V3)
    connection.close()
    with pytest.raises(MigrationError, match="expected database schema version 4"):
        inspect_v4_database(v3_path)

    v4_path = tmp_path / "v4.db"
    create_v4_database(v4_path)
    migrate_v4_to_v5(v4_path)
    with pytest.raises(MigrationError, match="expected database schema version 4"):
        migrate_v4_to_v5(v4_path)


@pytest.mark.parametrize("artifact", ["table", "column", "index"])
def test_v5_migration_rejects_partial_v5_artifacts(
    tmp_path: Path,
    artifact: str,
):
    database_path = tmp_path / f"partial-{artifact}.db"
    create_v4_database(database_path)
    connection = sqlite3.connect(database_path)
    if artifact == "table":
        connection.execute(CAPTURE_DRAFTS_TABLE_SQL)
    elif artifact == "column":
        connection.execute(
            "ALTER TABLE item_inputs ADD COLUMN source_draft_id INTEGER"
        )
    else:
        connection.execute(
            "CREATE UNIQUE INDEX idx_item_inputs_id_user "
            "ON item_inputs(id, user_id)"
        )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError, match="v5"):
        inspect_v4_database(database_path)


def test_v5_migration_rejects_source_foreign_key_failure(tmp_path: Path):
    database_path = tmp_path / "bad-fk-v4.db"
    create_v4_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        INSERT INTO user_sessions (
            user_id, token_hash, csrf_token_hash, created_time,
            last_seen_time, expires_time, revoked_time, user_agent
        ) VALUES (999, 'orphan-session', 'orphan-csrf', ?, ?, ?, NULL, 'test')
        """,
        (NOW, NOW, "2027-09-07T01:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError, match="foreign key"):
        inspect_v4_database(database_path)


def test_application_startup_rejects_unmigrated_v4_database(tmp_path: Path):
    database_path = tmp_path / "startup-v4.db"
    create_v4_database(database_path)
    before = logical_snapshot(database_path)

    with pytest.raises(DatabaseVersionError, match="explicit migration to version 6"):
        Database(database_path).initialize()

    assert logical_snapshot(database_path) == before
