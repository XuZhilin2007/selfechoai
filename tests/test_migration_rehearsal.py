from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.config import Settings
from app.database import (
    Database,
    DatabaseVersionError,
    SCHEMA_V3_VERSION,
    SCHEMA_VERSION,
)
from app.main import create_app
from app.migrations import v003_auth
from app.migrations.v003_auth import (
    MigrationError,
    OwnerUser,
    inspect_v2_database,
    main,
    migrate_v2_to_v3,
)
from app.migrations.v004_reminders import migrate_v3_to_v4
from app.services.ai import DisabledAIService


V2_SCHEMA = """
CREATE TABLE personal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    importance TEXT NOT NULL,
    urgency TEXT NOT NULL,
    deadline TEXT,
    estimated_time INTEGER,
    status TEXT NOT NULL,
    next_action TEXT,
    extra_information TEXT,
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL
);

CREATE TABLE item_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES personal_items(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL,
    input_method TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    failure_type TEXT,
    failure_message TEXT,
    created_time TEXT NOT NULL
);

PRAGMA user_version = 2;
"""


def create_v2_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(V2_SCHEMA)
    connection.execute(
        """
        INSERT INTO personal_items (
            id, title, type, importance, urgency, deadline, estimated_time,
            status, next_action, extra_information, created_time, updated_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            41,
            "需要保留 ID 的事项",
            "decision",
            "high",
            "medium",
            "2026-09-30",
            45,
            "active",
            "核对迁移结果",
            '{"constraint":"原始背景不能丢失"}',
            "2026-08-20T00:00:00+00:00",
            "2026-08-21T00:00:00+00:00",
        ),
    )
    connection.executemany(
        """
        INSERT INTO item_inputs (
            id, item_id, original_text, input_method, processing_status,
            failure_type, failure_message, created_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                73,
                41,
                "必须逐字保留的原始输入",
                "text",
                "succeeded",
                None,
                None,
                "2026-08-20T00:00:00+00:00",
            ),
            (
                79,
                None,
                "失败状态也必须保留",
                "voice",
                "failed",
                "network",
                "网络不可用",
                "2026-08-22T00:00:00+00:00",
            ),
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


def test_cli_migration_rehearsal_creates_login_ready_owner_and_preserves_data(
    tmp_path: Path,
):
    database_path = tmp_path / "rehearsal-v2.db"
    create_v2_database(database_path)
    answers = iter(
        [
            " OWNER@Example.com ",
            "Owner",
            "",
            "MIGRATE V2 TO V3",
        ]
    )
    passwords = iter(["owner rehearsal password", "owner rehearsal password"])
    output: list[str] = []

    exit_code = main(
        ["--database", str(database_path)],
        input_fn=lambda prompt: next(answers),
        password_fn=lambda prompt: next(passwords),
        output_fn=output.append,
    )

    assert exit_code == 0
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    owner = connection.execute("SELECT * FROM users").fetchone()
    item = connection.execute(
        "SELECT * FROM personal_items WHERE id = 41"
    ).fetchone()
    inputs = connection.execute(
        "SELECT * FROM item_inputs ORDER BY id"
    ).fetchall()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.close()

    assert version == SCHEMA_V3_VERSION
    assert owner["email"] == "owner@example.com"
    assert verify_password("owner rehearsal password", owner["password_hash"])
    assert item["id"] == 41
    assert item["user_id"] == owner["id"]
    assert [row["id"] for row in inputs] == [73, 79]
    assert inputs[0]["item_id"] == 41
    assert inputs[0]["original_text"] == "必须逐字保留的原始输入"
    assert inputs[0]["processing_status"] == "succeeded"
    assert inputs[1]["item_id"] is None
    assert inputs[1]["original_text"] == "失败状态也必须保留"
    assert inputs[1]["processing_status"] == "failed"
    assert inputs[1]["failure_type"] == "network"
    assert {row["user_id"] for row in inputs} == {owner["id"]}
    assert foreign_key_errors == []
    assert "Migration completed successfully." in output


def test_cli_check_only_is_read_only_and_has_no_password_argument(tmp_path: Path):
    database_path = tmp_path / "check-only-v2.db"
    create_v2_database(database_path)
    before = database_path.read_bytes()
    output: list[str] = []

    exit_code = main(
        ["--database", str(database_path), "--check-only"],
        input_fn=lambda prompt: pytest.fail("check-only must not prompt"),
        password_fn=lambda prompt: pytest.fail("check-only must not read a password"),
        output_fn=output.append,
    )

    parser = v003_auth._build_argument_parser()
    option_strings = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    assert exit_code == 0
    assert database_path.read_bytes() == before
    assert "--password" not in option_strings
    assert "Preflight passed. No database changes were made." in output


def test_migration_rolls_back_all_schema_and_data_changes_on_failure(
    tmp_path: Path,
    monkeypatch,
):
    database_path = tmp_path / "rollback-v2.db"
    create_v2_database(database_path)
    before = logical_snapshot(database_path)

    def force_failure(*args, **kwargs):
        raise MigrationError("forced rehearsal failure")

    monkeypatch.setattr(v003_auth, "_validate_copied_data", force_failure)
    with pytest.raises(MigrationError, match="forced rehearsal failure"):
        migrate_v2_to_v3(
            database_path,
            OwnerUser(
                email="owner@example.com",
                password_hash=hash_password("rollback test password"),
                display_name="Owner",
            ),
        )

    assert logical_snapshot(database_path) == before
    connection = sqlite3.connect(database_path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()
    assert version == 2
    assert "users" not in tables
    assert "_v2_personal_items" not in tables
    assert "_v2_item_inputs" not in tables


def test_unknown_schema_version_is_rejected_without_changes(tmp_path: Path):
    database_path = tmp_path / "unknown.db"
    create_v2_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    before = logical_snapshot(database_path)

    with pytest.raises(MigrationError, match="expected database schema version 2, got 99"):
        inspect_v2_database(database_path)
    with pytest.raises(MigrationError, match="expected database schema version 2, got 99"):
        migrate_v2_to_v3(
            database_path,
            OwnerUser(
                email="owner@example.com",
                password_hash=hash_password("unknown schema password"),
                display_name="Owner",
            ),
        )

    assert logical_snapshot(database_path) == before


def test_application_startup_rejects_v2_without_running_migration(tmp_path: Path):
    database_path = tmp_path / "startup-v2.db"
    create_v2_database(database_path)
    before = logical_snapshot(database_path)
    app = create_app(
        settings=Settings(database_path=database_path),
        ai_service=DisabledAIService(),
    )

    with pytest.raises(DatabaseVersionError, match="explicit migration"):
        with TestClient(app):
            pass

    assert logical_snapshot(database_path) == before


def test_health_endpoint_does_not_modify_database(tmp_path: Path):
    database_path = tmp_path / "health-v3.db"
    app = create_app(
        settings=Settings(database_path=database_path),
        ai_service=DisabledAIService(),
    )
    with TestClient(app) as client:
        before = logical_snapshot(database_path)

        response = client.get("/api/health")

        after = logical_snapshot(database_path)

    assert response.status_code == 200
    assert response.json() == {"message": "ok"}
    assert after == before


def test_legacy_v2_write_cannot_silently_use_v3_database(tmp_path: Path):
    database_path = tmp_path / "legacy-write.db"
    create_v2_database(database_path)
    migrate_v2_to_v3(
        database_path,
        OwnerUser(
            email="owner@example.com",
            password_hash=hash_password("legacy guard password"),
            display_name="Owner",
        ),
    )

    connection = sqlite3.connect(database_path)
    actual_version = connection.execute("PRAGMA user_version").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="user_id"):
        connection.execute(
            """
            INSERT INTO item_inputs (
                item_id, original_text, input_method,
                processing_status, created_time
            ) VALUES (NULL, 'legacy write', 'text', 'pending', ?)
            """,
            ("2026-08-25T00:00:00+00:00",),
        )
    connection.close()

    assert actual_version == 3
    assert actual_version != 2


def test_v2_to_v3_to_v4_migration_chain_preserves_data(tmp_path: Path):
    database_path = tmp_path / "v2-v3-v4-chain.db"
    create_v2_database(database_path)
    migrate_v2_to_v3(
        database_path,
        OwnerUser(
            email="owner@example.com",
            password_hash=hash_password("migration chain password"),
            display_name="Owner",
            timezone="Asia/Shanghai",
        ),
    )

    intermediate = sqlite3.connect(database_path)
    intermediate_version = intermediate.execute("PRAGMA user_version").fetchone()[0]
    intermediate.close()
    assert intermediate_version == SCHEMA_V3_VERSION

    migrate_v3_to_v4(database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    owner = connection.execute("SELECT * FROM users").fetchone()
    item = connection.execute(
        "SELECT * FROM personal_items WHERE id = 41"
    ).fetchone()
    inputs = connection.execute("SELECT * FROM item_inputs ORDER BY id").fetchall()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()

    assert version == SCHEMA_VERSION
    assert owner["default_reminder_time"] == "09:00"
    assert item["title"] == "需要保留 ID 的事项"
    assert item["extra_information"] == '{"constraint":"原始背景不能丢失"}'
    assert item["reminder_prompt_dismissed_at"] is None
    assert [row["id"] for row in inputs] == [73, 79]
    assert inputs[0]["original_text"] == "必须逐字保留的原始输入"
    assert inputs[1]["original_text"] == "失败状态也必须保留"
    assert foreign_key_errors == []
    assert integrity == "ok"
