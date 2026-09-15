from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_V6,
    V7_LIFECYCLE_COLUMNS_SQL,
    Database,
)
from app.migrations import v007_item_lifecycle
from app.migrations.v007_item_lifecycle import (
    EXISTING_TABLES,
    MigrationError,
    inspect_v6_database,
    migrate_v6_to_v7,
)
from app.repository import Repository
from app.schemas import ItemStatus, UserItemPatch


NOW = "2026-09-13T01:00:00+00:00"
MIGRATION_TIME = datetime(2026, 9, 13, 2, 3, 4, tzinfo=timezone.utc)


def create_populated_v6_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_V6)
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
    for item_id, status in enumerate(("active", "completed", "trash"), start=1):
        connection.execute(
            """
            INSERT INTO personal_items (
                id, user_id, title, type, importance, urgency, status,
                created_time, updated_time
            ) VALUES (?, 1, ?, 'task', 'medium', 'low', ?, ?, ?)
            """,
            (item_id, f"legacy-{status}", status, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO item_inputs (
                id, user_id, item_id, original_text, input_method,
                processing_status, created_time
            ) VALUES (?, 1, ?, ?, 'text', 'succeeded', ?)
            """,
            (item_id, item_id, f"original-{status}", NOW),
        )
    connection.commit()
    connection.close()


def row_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in EXISTING_TABLES
        }
    finally:
        connection.close()


def index_columns(path: Path, index_name: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info({index_name})")
        ]
    finally:
        connection.close()


def test_check_only_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    path = tmp_path / "check-only-v6.db"
    create_populated_v6_database(path)
    before = path.read_bytes()

    preflight = inspect_v6_database(path)

    assert preflight.schema_version == 6
    assert preflight.lifecycle_counts == {"active": 1, "completed": 1, "trash": 1}
    assert path.read_bytes() == before


def test_v6_to_v7_preserves_rows_and_applies_exact_legacy_policy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle-v6.db"
    create_populated_v6_database(path)
    before = row_counts(path)

    result = migrate_v6_to_v7(path, migration_time=MIGRATION_TIME)

    assert result.row_counts == before
    assert result.migration_time == "2026-09-13T02:03:04+00:00"
    assert row_counts(path) == before
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        rows = {
            row["status"]: row
            for row in connection.execute(
                """
                SELECT status, completed_at, trashed_at, status_before_trash
                FROM personal_items
                """
            )
        }
        assert tuple(rows["active"])[1:] == (None, None, None)
        assert tuple(rows["completed"])[1:] == (None, None, None)
        assert tuple(rows["trash"])[1:] == (
            None,
            "2026-09-13T02:03:04+00:00",
            None,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE personal_items SET status_before_trash = 'trash' WHERE id = 1"
            )
    finally:
        connection.close()
    assert CURRENT_SCHEMA_VERSION == 7

    restored_legacy_trash = Repository(Database(path)).update_item(
        3,
        1,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert restored_legacy_trash.status == ItemStatus.ACTIVE
    assert restored_legacy_trash.completed_at is None
    assert restored_legacy_trash.trashed_at is None
    assert restored_legacy_trash.status_before_trash is None


def test_fresh_and_migrated_v7_share_retention_index_definition(
    tmp_path: Path,
) -> None:
    migrated_path = tmp_path / "migrated-v7.db"
    fresh_path = tmp_path / "fresh-v7.db"
    create_populated_v6_database(migrated_path)
    migrate_v6_to_v7(migrated_path, migration_time=MIGRATION_TIME)
    Database(fresh_path).initialize()

    for path in (migrated_path, fresh_path):
        assert index_columns(
            path,
            "idx_personal_items_trash_retention",
        ) == ["status", "trashed_at", "id"]
        Database(path).initialize()


def test_migration_rolls_back_schema_and_data_after_injected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback-v6.db"
    create_populated_v6_database(path)
    before = row_counts(path)

    def partially_apply_then_fail(connection: sqlite3.Connection) -> None:
        connection.execute(V7_LIFECYCLE_COLUMNS_SQL[0])
        raise RuntimeError("simulated lifecycle migration failure")

    monkeypatch.setattr(
        v007_item_lifecycle,
        "_apply_schema_changes",
        partially_apply_then_fail,
    )
    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v6_to_v7(path, migration_time=MIGRATION_TIME)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(personal_items)")
        }
        assert not {"completed_at", "trashed_at", "status_before_trash"} & columns
    finally:
        connection.close()
    assert row_counts(path) == before


def test_migration_refuses_rerun_and_cli_requires_exact_backup_acknowledgement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rerun-v6.db"
    create_populated_v6_database(path)
    output: list[str] = []

    cancelled = v007_item_lifecycle.main(
        ["--database", str(path)],
        input_fn=lambda _prompt: "no",
        output_fn=output.append,
    )

    assert cancelled == 2
    assert any("validated, recoverable schema v6 backup" in line for line in output)
    assert inspect_v6_database(path).schema_version == 6
    migrate_v6_to_v7(path, migration_time=MIGRATION_TIME)
    with pytest.raises(MigrationError, match="expected database schema version 6"):
        inspect_v6_database(path)
    with pytest.raises(MigrationError, match="expected database schema version 6"):
        migrate_v6_to_v7(path, migration_time=MIGRATION_TIME)
