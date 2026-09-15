from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.database import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_V6_VERSION,
    V7_INDEXES_SQL,
    V7_LIFECYCLE_COLUMNS_SQL,
    Database,
)
from app.time_utils import serialize_utc_datetime


EXISTING_TABLES = (
    "users",
    "personal_items",
    "item_inputs",
    "user_sessions",
    "reminders",
    "push_subscriptions",
    "reminder_deliveries",
    "capture_drafts",
    "voice_segments",
    "voice_file_deletions",
    "email_reminder_settings",
    "email_verification_challenges",
    "reminder_email_deliveries",
)
NEW_COLUMNS = ("completed_at", "trashed_at", "status_before_trash")
NEW_INDEXES = (
    "idx_personal_items_lifecycle",
    "idx_personal_items_trash_retention",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    row_counts: dict[str, int]
    lifecycle_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    row_counts: dict[str, int]
    lifecycle_counts: dict[str, int]
    migration_time: str


def inspect_v6_database(database_path: Path) -> MigrationPreflight:
    """Validate a v6 database through a strictly read-only connection."""

    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise MigrationError(f"database file does not exist: {resolved}")
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        _validate_source_database(connection)
        return _preflight(connection, resolved)
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite preflight failed: {exc}") from exc
    finally:
        connection.close()


def migrate_v6_to_v7(
    database_path: Path,
    *,
    migration_time: datetime | None = None,
) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up v6 database to schema v7."""

    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise MigrationError(f"database file does not exist: {resolved}")
    migration_timestamp = serialize_utc_datetime(
        migration_time or datetime.now(timezone.utc),
        field_name="migration_time",
    )
    try:
        connection = sqlite3.connect(resolved, timeout=10)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database for migration: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _validate_source_database(connection)
        preflight = _preflight(connection, resolved)
        connection.execute("BEGIN IMMEDIATE")
        try:
            _apply_schema_changes(connection)
            connection.execute(
                """
                UPDATE personal_items
                SET trashed_at = ?
                WHERE status = 'trash'
                """,
                (migration_timestamp,),
            )
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            _validate_migrated_data(
                connection,
                preflight,
                migration_timestamp=migration_timestamp,
            )
            Database._validate_v7_schema(connection, _table_names(connection))
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("foreign key validation failed after migration")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MigrationError(
                    f"database integrity check failed after migration: {integrity}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return MigrationResult(
            row_counts=dict(preflight.row_counts),
            lifecycle_counts=dict(preflight.lifecycle_counts),
            migration_time=migration_timestamp,
        )
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite migration failed: {exc}") from exc
    except Exception as exc:
        raise MigrationError(
            f"migration aborted and transaction was rolled back: {exc}"
        ) from exc
    finally:
        connection.close()


def _preflight(
    connection: sqlite3.Connection,
    database_path: Path,
) -> MigrationPreflight:
    return MigrationPreflight(
        database_path=database_path,
        schema_version=SCHEMA_V6_VERSION,
        row_counts={name: _row_count(connection, name) for name in EXISTING_TABLES},
        lifecycle_counts={
            status: int(
                connection.execute(
                    "SELECT COUNT(*) FROM personal_items WHERE status = ?",
                    (status,),
                ).fetchone()[0]
            )
            for status in ("active", "completed", "trash")
        },
    )


def _validate_source_database(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V6_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V6_VERSION}, got {version}"
        )
    tables = _table_names(connection)
    try:
        Database._validate_v6_schema(connection, tables)
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(personal_items)")
    }
    unexpected_columns = set(NEW_COLUMNS) & columns
    if unexpected_columns:
        raise MigrationError(
            "version 6 database contains v7 columns: "
            + ", ".join(sorted(unexpected_columns))
        )
    unexpected_indexes = set(NEW_INDEXES) & _index_names(connection)
    if unexpected_indexes:
        raise MigrationError(
            "version 6 database contains v7 indexes: "
            + ", ".join(sorted(unexpected_indexes))
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"source database integrity check failed: {integrity}")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("source database foreign key validation failed")


def _apply_schema_changes(connection: sqlite3.Connection) -> None:
    for statement in V7_LIFECYCLE_COLUMNS_SQL:
        connection.execute(statement)
    _execute_statements(connection, V7_INDEXES_SQL)


def _validate_migrated_data(
    connection: sqlite3.Connection,
    preflight: MigrationPreflight,
    *,
    migration_timestamp: str,
) -> None:
    for table_name, expected_count in preflight.row_counts.items():
        if _row_count(connection, table_name) != expected_count:
            raise MigrationError(f"{table_name} row count changed during migration")
    for status, expected_count in preflight.lifecycle_counts.items():
        actual_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM personal_items WHERE status = ?",
                (status,),
            ).fetchone()[0]
        )
        if actual_count != expected_count:
            raise MigrationError(
                f"{status} item count changed during migration"
            )

    active_invalid = connection.execute(
        """
        SELECT COUNT(*) FROM personal_items
        WHERE status = 'active'
          AND (completed_at IS NOT NULL OR trashed_at IS NOT NULL
               OR status_before_trash IS NOT NULL)
        """
    ).fetchone()[0]
    completed_invalid = connection.execute(
        """
        SELECT COUNT(*) FROM personal_items
        WHERE status = 'completed'
          AND (completed_at IS NOT NULL OR trashed_at IS NOT NULL
               OR status_before_trash IS NOT NULL)
        """
    ).fetchone()[0]
    trash_invalid = connection.execute(
        """
        SELECT COUNT(*) FROM personal_items
        WHERE status = 'trash'
          AND (completed_at IS NOT NULL OR trashed_at IS NULL
               OR trashed_at != ?
               OR status_before_trash IS NOT NULL)
        """,
        (migration_timestamp,),
    ).fetchone()[0]
    if active_invalid or completed_invalid or trash_invalid:
        raise MigrationError("legacy lifecycle metadata policy validation failed")


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _execute_statements(connection: sqlite3.Connection, sql: str) -> None:
    for statement in sql.split(";"):
        if statement.strip():
            connection.execute(statement)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly migrate a stopped SelfEcho schema v6 database to v7."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run read-only preflight checks without changing the database",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    try:
        preflight = inspect_v6_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        for table_name, count in preflight.row_counts.items():
            output_fn(f"{table_name}: {count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0
        output_fn("A validated, recoverable schema v6 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE V6 TO V7" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE V6 TO V7":
            output_fn("Migration cancelled. No database changes were made.")
            return 2
        result = migrate_v6_to_v7(preflight.database_path)
        output_fn("Migration completed successfully.")
        for table_name, count in result.row_counts.items():
            output_fn(f"{table_name} preserved: {count}")
        output_fn(f"Schema version is now {CURRENT_SCHEMA_VERSION}.")
        return 0
    except (MigrationError, ValueError) as exc:
        output_fn(f"ERROR: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        output_fn("Migration cancelled. No database changes were requested.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
