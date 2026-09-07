from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    CAPTURE_DRAFTS_TABLE_SQL,
    Database,
    REQUIRED_V4_INDEXES,
    REQUIRED_V5_INDEXES,
    SCHEMA_V4_VERSION,
    SCHEMA_VERSION,
    V5_INDEXES_SQL,
    V5_PRE_VOICE_INDEXES_SQL,
    VOICE_FILE_DELETIONS_TABLE_SQL,
    VOICE_SEGMENTS_TABLE_SQL,
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    user_count: int
    personal_item_count: int
    item_input_count: int
    user_session_count: int
    reminder_count: int
    push_subscription_count: int
    reminder_delivery_count: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    user_count: int
    personal_item_count: int
    item_input_count: int
    user_session_count: int
    reminder_count: int
    push_subscription_count: int
    reminder_delivery_count: int


def inspect_v4_database(database_path: Path) -> MigrationPreflight:
    """Validate a Community v4 database through a read-only connection."""

    database_path = database_path.expanduser().resolve()
    if not database_path.is_file():
        raise MigrationError(f"database file does not exist: {database_path}")
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        _validate_source_database(connection)
        return _preflight_from_connection(database_path, connection)
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite preflight failed: {exc}") from exc
    finally:
        connection.close()


def migrate_v4_to_v5(database_path: Path) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up Community v4 database to v5."""

    database_path = database_path.expanduser().resolve()
    if not database_path.is_file():
        raise MigrationError(f"database file does not exist: {database_path}")
    try:
        connection = sqlite3.connect(database_path, timeout=10)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database for migration: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        _validate_source_database(connection)
        preflight = _preflight_from_connection(database_path, connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                ALTER TABLE item_inputs
                ADD COLUMN source_draft_id INTEGER
                CHECK (source_draft_id IS NULL OR source_draft_id > 0)
                """
            )
            connection.execute(CAPTURE_DRAFTS_TABLE_SQL)
            _execute_statements(connection, V5_PRE_VOICE_INDEXES_SQL)
            connection.execute(VOICE_SEGMENTS_TABLE_SQL)
            connection.execute(VOICE_FILE_DELETIONS_TABLE_SQL)
            _execute_statements(connection, V5_INDEXES_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            _validate_migrated_data(connection, preflight)
            Database._validate_v5_schema(connection, _table_names(connection))
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
            user_count=preflight.user_count,
            personal_item_count=preflight.personal_item_count,
            item_input_count=preflight.item_input_count,
            user_session_count=preflight.user_session_count,
            reminder_count=preflight.reminder_count,
            push_subscription_count=preflight.push_subscription_count,
            reminder_delivery_count=preflight.reminder_delivery_count,
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


def _validate_source_database(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V4_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V4_VERSION}, got {version}"
        )

    tables = _table_names(connection)
    try:
        Database._validate_v4_schema(connection, tables)
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc

    unexpected_tables = {
        "capture_drafts",
        "voice_segments",
        "voice_file_deletions",
    } & tables
    if unexpected_tables:
        names = ", ".join(sorted(unexpected_tables))
        raise MigrationError(f"version 4 database contains v5 tables: {names}")

    item_input_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(item_inputs)")
    }
    if "source_draft_id" in item_input_columns:
        raise MigrationError("version 4 item_inputs contains the v5 source_draft_id")

    existing_indexes = _index_names(connection)
    unexpected_indexes = (REQUIRED_V5_INDEXES - REQUIRED_V4_INDEXES) & existing_indexes
    if unexpected_indexes:
        names = ", ".join(sorted(unexpected_indexes))
        raise MigrationError(f"version 4 database contains v5 indexes: {names}")

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"source database integrity check failed: {integrity}")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("source database foreign key validation failed")


def _validate_migrated_data(
    connection: sqlite3.Connection,
    preflight: MigrationPreflight,
) -> None:
    expected_counts = {
        "users": preflight.user_count,
        "personal_items": preflight.personal_item_count,
        "item_inputs": preflight.item_input_count,
        "user_sessions": preflight.user_session_count,
        "reminders": preflight.reminder_count,
        "push_subscriptions": preflight.push_subscription_count,
        "reminder_deliveries": preflight.reminder_delivery_count,
    }
    for table_name, expected_count in expected_counts.items():
        if _row_count(connection, table_name) != expected_count:
            raise MigrationError(f"{table_name} row count changed during migration")

    for table_name in ("capture_drafts", "voice_segments", "voice_file_deletions"):
        if _row_count(connection, table_name) != 0:
            raise MigrationError(f"new table {table_name} was not empty")

    legacy_sources = connection.execute(
        "SELECT COUNT(*) FROM item_inputs WHERE source_draft_id IS NOT NULL"
    ).fetchone()[0]
    if legacy_sources:
        raise MigrationError("legacy item_inputs source_draft_id backfill is not NULL")


def _preflight_from_connection(
    database_path: Path,
    connection: sqlite3.Connection,
) -> MigrationPreflight:
    return MigrationPreflight(
        database_path=database_path,
        schema_version=SCHEMA_V4_VERSION,
        user_count=_row_count(connection, "users"),
        personal_item_count=_row_count(connection, "personal_items"),
        item_input_count=_row_count(connection, "item_inputs"),
        user_session_count=_row_count(connection, "user_sessions"),
        reminder_count=_row_count(connection, "reminders"),
        push_subscription_count=_row_count(connection, "push_subscriptions"),
        reminder_delivery_count=_row_count(connection, "reminder_deliveries"),
    )


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
        description="Explicitly migrate a stopped SelfEcho schema v4 database to v5."
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Path to the stopped schema v4 SQLite database",
    )
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
        preflight = inspect_v4_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        output_fn(f"Users: {preflight.user_count}")
        output_fn(f"Personal Items: {preflight.personal_item_count}")
        output_fn(f"Item Inputs: {preflight.item_input_count}")
        output_fn(f"User Sessions: {preflight.user_session_count}")
        output_fn(f"Reminders: {preflight.reminder_count}")
        output_fn(f"Push Subscriptions: {preflight.push_subscription_count}")
        output_fn(f"Reminder Deliveries: {preflight.reminder_delivery_count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0

        output_fn("A validated, recoverable schema v4 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE V4 TO V5" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE V4 TO V5":
            output_fn("Migration cancelled. No database changes were made.")
            return 2

        result = migrate_v4_to_v5(preflight.database_path)
        output_fn("Migration completed successfully.")
        output_fn(f"Users preserved: {result.user_count}")
        output_fn(f"Personal Items preserved: {result.personal_item_count}")
        output_fn(f"Item Inputs preserved: {result.item_input_count}")
        output_fn(f"User Sessions preserved: {result.user_session_count}")
        output_fn(f"Reminders preserved: {result.reminder_count}")
        output_fn(f"Push Subscriptions preserved: {result.push_subscription_count}")
        output_fn(f"Reminder Deliveries preserved: {result.reminder_delivery_count}")
        output_fn(f"Schema version is now {SCHEMA_VERSION}.")
        return 0
    except (MigrationError, ValueError) as exc:
        output_fn(f"ERROR: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        output_fn("Migration cancelled. No database changes were requested.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
