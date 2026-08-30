from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.auth_repository import AuthRepository
from app.database import (
    Database,
    DatabaseVersionError,
    REQUIRED_V4_INDEXES,
    SCHEMA_V3,
    SCHEMA_V3_VERSION,
    SCHEMA_VERSION,
)
from app.migrations import v004_reminders
from app.migrations.v004_reminders import (
    MigrationError,
    inspect_v3_database,
    main,
    migrate_v3_to_v4,
)


NOW = "2026-08-30T00:00:00+00:00"


def create_v3_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_V3)
    connection.executemany(
        """
        INSERT INTO users (
            id, email, password_hash, display_name, timezone, status,
            password_changed_time, created_time, updated_time
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            (
                1,
                "owner@example.com",
                "$argon2id$synthetic-owner-hash",
                "Owner",
                "Asia/Shanghai",
                NOW,
                NOW,
                NOW,
            ),
            (
                2,
                "second@example.com",
                "$argon2id$synthetic-second-hash",
                "Second User",
                "Europe/London",
                NOW,
                NOW,
                NOW,
            ),
        ),
    )
    connection.executemany(
        """
        INSERT INTO personal_items (
            id, user_id, title, type, importance, urgency, deadline,
            estimated_time, status, next_action, extra_information,
            created_time, updated_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                10,
                1,
                "保留事项",
                "decision",
                "high",
                "medium",
                "2026-09-01",
                30,
                "active",
                "核对迁移",
                '{"constraint":"原始背景必须保留"}',
                NOW,
                NOW,
            ),
            (
                20,
                2,
                "第二位用户的事项",
                "note",
                "unknown",
                "low",
                None,
                None,
                "completed",
                None,
                None,
                NOW,
                NOW,
            ),
        ),
    )
    connection.executemany(
        """
        INSERT INTO item_inputs (
            id, user_id, item_id, original_text, input_method,
            processing_status, failure_type, failure_message, created_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                100,
                1,
                10,
                "这段原始输入必须逐字保留",
                "text",
                "succeeded",
                None,
                None,
                NOW,
            ),
            (
                200,
                2,
                None,
                "尚未关联的原始输入",
                "voice",
                "failed",
                "network",
                "synthetic network failure",
                NOW,
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO user_sessions (
            id, user_id, token_hash, csrf_token_hash, created_time,
            last_seen_time, expires_time, revoked_time, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            300,
            1,
            "synthetic-session-token-hash",
            "synthetic-csrf-token-hash",
            NOW,
            NOW,
            "2026-09-30T00:00:00+00:00",
            None,
            "Synthetic migration test client",
        ),
    )
    connection.commit()
    connection.close()


def logical_snapshot(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_v3_preflight_is_read_only(tmp_path: Path):
    database_path = tmp_path / "preflight-v3.db"
    create_v3_database(database_path)
    before = database_path.read_bytes()

    preflight = inspect_v3_database(database_path)

    assert preflight.schema_version == SCHEMA_V3_VERSION
    assert preflight.user_count == 2
    assert preflight.personal_item_count == 2
    assert preflight.item_input_count == 2
    assert preflight.user_session_count == 1
    assert database_path.read_bytes() == before


def test_v3_database_migrates_to_v4_without_losing_community_data(tmp_path: Path):
    database_path = tmp_path / "community-v3.db"
    create_v3_database(database_path)

    result = migrate_v3_to_v4(database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    users = connection.execute("SELECT * FROM users ORDER BY id").fetchall()
    items = connection.execute(
        "SELECT * FROM personal_items ORDER BY id"
    ).fetchall()
    inputs = connection.execute("SELECT * FROM item_inputs ORDER BY id").fetchall()
    sessions = connection.execute(
        "SELECT * FROM user_sessions ORDER BY id"
    ).fetchall()
    new_table_counts = {
        table_name: connection.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()[0]
        for table_name in (
            "reminders",
            "push_subscriptions",
            "reminder_deliveries",
        )
    }
    indexes = {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()

    assert result.user_count == len(users) == 2
    assert result.personal_item_count == len(items) == 2
    assert result.item_input_count == len(inputs) == 2
    assert result.user_session_count == len(sessions) == 1
    assert [row["email"] for row in users] == [
        "owner@example.com",
        "second@example.com",
    ]
    assert [row["default_reminder_time"] for row in users] == ["09:00", "09:00"]
    assert items[0]["title"] == "保留事项"
    assert items[0]["extra_information"] == '{"constraint":"原始背景必须保留"}'
    assert all(row["reminder_prompt_dismissed_at"] is None for row in items)
    assert inputs[0]["original_text"] == "这段原始输入必须逐字保留"
    assert inputs[0]["user_id"] == items[0]["user_id"] == 1
    assert inputs[1]["item_id"] is None
    assert inputs[1]["failure_type"] == "network"
    assert sessions[0]["id"] == 300
    assert sessions[0]["user_id"] == 1
    assert sessions[0]["token_hash"] == "synthetic-session-token-hash"
    assert new_table_counts == {
        "reminders": 0,
        "push_subscriptions": 0,
        "reminder_deliveries": 0,
    }
    assert REQUIRED_V4_INDEXES <= indexes
    assert version == SCHEMA_VERSION
    assert foreign_key_errors == []
    assert integrity == "ok"

    database = Database(database_path)
    database.initialize()
    migrated_session = AuthRepository(database).get_session_by_token_hash(
        "synthetic-session-token-hash"
    )
    assert migrated_session is not None
    assert migrated_session.id == 300
    assert migrated_session.user_id == 1


def test_v3_to_v4_migration_rolls_back_schema_and_data_on_failure(
    tmp_path: Path,
    monkeypatch,
):
    database_path = tmp_path / "rollback-v3.db"
    create_v3_database(database_path)
    before = logical_snapshot(database_path)

    def force_failure(*args, **kwargs):
        raise MigrationError("forced v4 validation failure")

    monkeypatch.setattr(v004_reminders, "_validate_migrated_data", force_failure)
    with pytest.raises(MigrationError, match="forced v4 validation failure"):
        migrate_v3_to_v4(database_path)

    assert logical_snapshot(database_path) == before
    connection = sqlite3.connect(database_path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    user_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(users)")
    }
    item_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(personal_items)")
    }
    owner_email = connection.execute(
        "SELECT email FROM users WHERE id = 1"
    ).fetchone()[0]
    original_text = connection.execute(
        "SELECT original_text FROM item_inputs WHERE id = 100"
    ).fetchone()[0]
    connection.close()

    assert version == SCHEMA_V3_VERSION
    assert "reminders" not in tables
    assert "push_subscriptions" not in tables
    assert "reminder_deliveries" not in tables
    assert "default_reminder_time" not in user_columns
    assert "reminder_prompt_dismissed_at" not in item_columns
    assert owner_email == "owner@example.com"
    assert original_text == "这段原始输入必须逐字保留"


def test_v3_migration_cli_check_only_and_cancel_are_read_only(tmp_path: Path):
    database_path = tmp_path / "cli-v3.db"
    create_v3_database(database_path)
    before = database_path.read_bytes()
    output: list[str] = []
    prompts: list[str] = []

    def cancel_migration(prompt: str) -> str:
        prompts.append(prompt)
        return "cancel"

    check_exit_code = main(
        ["--database", str(database_path), "--check-only"],
        input_fn=lambda prompt: pytest.fail("check-only must not prompt"),
        output_fn=output.append,
    )
    cancel_exit_code = main(
        ["--database", str(database_path)],
        input_fn=cancel_migration,
        output_fn=output.append,
    )

    assert check_exit_code == 0
    assert cancel_exit_code == 2
    assert database_path.read_bytes() == before
    assert prompts == ['Type "MIGRATE V3 TO V4" to execute the transaction: ']
    assert "Preflight passed. No database changes were made." in output
    assert "Migration cancelled. No database changes were made." in output


def test_application_startup_rejects_v3_without_silent_migration(tmp_path: Path):
    database_path = tmp_path / "startup-v3.db"
    create_v3_database(database_path)
    before = logical_snapshot(database_path)

    with pytest.raises(DatabaseVersionError, match="explicit migration"):
        Database(database_path).initialize()

    assert logical_snapshot(database_path) == before


def test_v3_preflight_rejects_invalid_timezone_without_changes(tmp_path: Path):
    database_path = tmp_path / "invalid-timezone-v3.db"
    create_v3_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("UPDATE users SET timezone = 'Not/A_Real_Timezone' WHERE id = 1")
    connection.commit()
    connection.close()
    before = logical_snapshot(database_path)

    with pytest.raises(MigrationError, match="invalid IANA timezone"):
        inspect_v3_database(database_path)

    assert logical_snapshot(database_path) == before
