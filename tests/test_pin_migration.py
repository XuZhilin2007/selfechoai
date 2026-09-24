import sqlite3

import pytest

from app.database import (
    CURRENT_SCHEMA_VERSION, SCHEMA_VERSION, SCHEMA_V7, SCHEMA_V7_VERSION,
    V8_PIN_COLUMN_SQL, Database, DatabaseSchemaError, DatabaseVersionError,
)
from app.migrations import v007_item_lifecycle, v008_item_pin as migration
from tests.test_lifecycle_migration import create_populated_v6_database, MIGRATION_TIME


def create_v7(path):
    create_populated_v6_database(path)
    v007_item_lifecycle.migrate_v6_to_v7(path, migration_time=MIGRATION_TIME)
    with sqlite3.connect(path) as connection:
        for item_id, deadline in enumerate(("2026-09-20", "2026-09-19T00:00:00", "2026-09-18T23:30:00+00:00"), 1):
            connection.execute("UPDATE personal_items SET deadline = ?, extra_information = ? WHERE id = ?",
                               (deadline, '{"context":"original facts"}', item_id))
        connection.execute("UPDATE personal_items SET completed_at = ? WHERE id = 2", (MIGRATION_TIME.isoformat(),))
        connection.execute("UPDATE personal_items SET status_before_trash = 'active' WHERE id = 3")
        connection.execute("""INSERT INTO reminders
            (id, user_id, item_id, source_expression, scheduled_timezone, status, created_time, updated_time)
            VALUES (1, 1, 1, 'later', 'Asia/Shanghai', 'needs_confirmation', ?, ?)""",
                           (MIGRATION_TIME.isoformat(), MIGRATION_TIME.isoformat()))


def snapshot(path):
    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {name: ([row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')],
                       connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()) for name in names}


def test_v7_to_v8_preserves_every_original_value_and_defaults_false(tmp_path):
    path = tmp_path / "migrate.db"
    create_v7(path)
    before = snapshot(path)
    result = migration.migrate_v7_to_v8(path)
    assert result.schema_version == 8
    after = snapshot(path)
    assert before.keys() == after.keys()
    for name, (columns, rows) in before.items():
        if name == "personal_items":
            assert after[name][0] == columns + ["is_pinned"]
            assert after[name][1] == [row + (0,) for row in rows]
        else:
            assert after[name] == (columns, rows)
        assert result.row_counts[name] == len(rows)
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        # The existing INSERT column list can omit Pin for new Items.
        connection.execute("""INSERT INTO personal_items
            (user_id, title, type, importance, urgency, status, created_time, updated_time)
            VALUES (1, 'new', 'other', 'unknown', 'unknown', 'active', 'now', 'now')""")
        assert connection.execute("SELECT is_pinned FROM personal_items WHERE title='new'").fetchone()[0] == 0
        connection.execute("UPDATE personal_items SET is_pinned = 1")
        for invalid in (None, -1, 2, 0.5, "true"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE personal_items SET is_pinned = ?", (invalid,))


def test_fresh_and_migrated_schema_match_and_historical_versions_stay_fixed(tmp_path):
    fresh, migrated = tmp_path / "fresh.db", tmp_path / "migrated.db"
    Database(fresh).initialize()
    create_v7(migrated)
    migration.migrate_v7_to_v8(migrated)
    def schema(path):
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
            return connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert schema(fresh) == schema(migrated)
    assert CURRENT_SCHEMA_VERSION == 8
    assert SCHEMA_V7_VERSION == 7
    assert SCHEMA_VERSION == 5
    historic = tmp_path / "historic.db"
    with sqlite3.connect(historic) as connection:
        connection.executescript(SCHEMA_V7)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "is_pinned" not in {row[1] for row in connection.execute("PRAGMA table_info(personal_items)")}
    with pytest.raises(DatabaseVersionError, match="explicit migration to version 8"):
        Database(historic).initialize()
    assert migration.inspect_v7_database(historic).schema_version == 7


def test_preflight_and_cancel_are_byte_for_byte_read_only(tmp_path):
    path = tmp_path / "preflight.db"
    create_v7(path)
    before = path.read_bytes()
    assert migration.inspect_v7_database(path).schema_version == 7
    output = []
    assert migration.main(["--database", str(path), "--check-only"],
                          input_fn=lambda _: pytest.fail("must not prompt"), output_fn=output.append) == 0
    assert migration.main(["--database", str(path)], input_fn=lambda _: "no", output_fn=output.append) == 2
    assert path.read_bytes() == before
    assert any("recoverable schema v7 backup" in line for line in output)
    assert migration.main(["--database", str(path)], input_fn=lambda _: "MIGRATE PUBLIC V7 TO V8", output_fn=output.append) == 0
    assert any("Schema version is now 8" in line for line in output)
    with pytest.raises(migration.MigrationError, match="expected database schema version 7"):
        migration.migrate_v7_to_v8(path)


@pytest.mark.parametrize("version", [0, 3, 5, 6, 8, 9])
def test_wrong_version_never_upgrades(tmp_path, version):
    path = tmp_path / "wrong.db"
    create_v7(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    for operation in (migration.inspect_v7_database, migration.migrate_v7_to_v8):
        with pytest.raises(migration.MigrationError, match="expected database schema version 7"):
            operation(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["index", "column", "missing_column", "foreign_key", "integrity"])
def test_invalid_v7_is_rejected_without_changes(tmp_path, damage):
    path = tmp_path / "invalid.db"
    create_v7(path)
    with sqlite3.connect(path) as connection:
        if damage == "index":
            connection.execute("DROP INDEX idx_personal_items_trash_retention")
        elif damage == "column":
            connection.execute(V8_PIN_COLUMN_SQL)
        elif damage == "missing_column":
            connection.execute("ALTER TABLE personal_items RENAME COLUMN deadline TO missing_deadline")
        elif damage == "foreign_key":
            connection.execute("UPDATE item_inputs SET user_id = 999")
        else:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE personal_items SET status_before_trash = 'invalid'")
    before = path.read_bytes()
    # SQLite's read-only integrity_check does not evaluate CHECK constraints.
    # The transactional rw validation must still reject this corruption.
    operations = (migration.migrate_v7_to_v8,) if damage == "integrity" else (
        migration.inspect_v7_database, migration.migrate_v7_to_v8,
    )
    for operation in operations:
        with pytest.raises(migration.MigrationError):
            operation(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["ddl", "validation", "pin", "row_count"])
def test_failure_rolls_back_schema_version_and_data(tmp_path, monkeypatch, failure):
    path = tmp_path / "rollback.db"
    create_v7(path)
    before = snapshot(path)
    original = migration._apply_schema_changes
    def faulty(connection):
        original(connection)
        if failure == "ddl":
            raise RuntimeError("injected DDL failure")
        if failure == "pin":
            connection.execute("UPDATE personal_items SET is_pinned = 1 WHERE id = 1")
        if failure == "row_count":
            connection.execute("DELETE FROM item_inputs WHERE id = 1")
    monkeypatch.setattr(migration, "_apply_schema_changes", faulty)
    if failure == "validation":
        def fail_validation(connection, before):
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
            raise RuntimeError("injected validation failure")
        monkeypatch.setattr(migration, "_validate_migrated_data", fail_validation)
    with pytest.raises(migration.MigrationError):
        migration.migrate_v7_to_v8(path)
    assert snapshot(path) == before
    assert migration.inspect_v7_database(path).schema_version == 7


@pytest.mark.parametrize("definition", ["INTEGER", "INTEGER NOT NULL DEFAULT 1 CHECK(is_pinned IN (0,1))", "INTEGER NOT NULL DEFAULT 0"])
def test_v8_startup_rejects_broken_pin_contract(tmp_path, definition):
    path = tmp_path / "malformed-v8.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_V7)
        connection.execute(f"ALTER TABLE personal_items ADD COLUMN is_pinned {definition}")
        connection.execute("PRAGMA user_version = 8")
    with pytest.raises(DatabaseSchemaError, match="boolean contract"):
        Database(path).initialize()


def test_missing_path_is_not_created_and_execution_rechecks_source(tmp_path):
    missing = tmp_path / "absent.db"
    for operation in (migration.inspect_v7_database, migration.migrate_v7_to_v8):
        with pytest.raises(migration.MigrationError, match="does not exist"):
            operation(missing)
    assert not missing.exists()
    path = tmp_path / "changed.db"
    create_v7(path)
    def changed_during_confirmation(_prompt):
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 6")
        return "MIGRATE PUBLIC V7 TO V8"
    assert migration.main(["--database", str(path)], input_fn=changed_during_confirmation,
                          output_fn=lambda _: None) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "is_pinned" not in {row[1] for row in connection.execute("PRAGMA table_info(personal_items)")}


def test_historical_v007_cli_still_reports_and_produces_v7(tmp_path):
    path = tmp_path / "historical-cli.db"
    create_populated_v6_database(path)
    output = []
    assert v007_item_lifecycle.main(["--database", str(path)],
                                   input_fn=lambda _: "MIGRATE PUBLIC V6 TO V7", output_fn=output.append) == 0
    assert "Schema version is now 7." in output
    assert migration.inspect_v7_database(path).schema_version == 7
    with pytest.raises(DatabaseVersionError):
        Database(path).initialize()
    migration.migrate_v7_to_v8(path)
    Database(path).initialize()
