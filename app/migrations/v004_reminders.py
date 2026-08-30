from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    Database,
    PUSH_SUBSCRIPTIONS_TABLE_SQL,
    REMINDER_DELIVERIES_TABLE_SQL,
    REMINDERS_TABLE_SQL,
    SCHEMA_V3_VERSION,
    SCHEMA_VERSION,
    USER_SESSIONS_OWNERSHIP_INDEX_SQL,
    V4_INDEXES_SQL,
)
from app.time_utils import validate_timezone_name


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


@dataclass(frozen=True, slots=True)
class MigrationResult:
    user_count: int
    personal_item_count: int
    item_input_count: int
    user_session_count: int


def inspect_v3_database(database_path: Path) -> MigrationPreflight:
    """Validate a Community schema-v3 database through a read-only connection."""

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


def migrate_v3_to_v4(database_path: Path) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up Community v3 database to v4."""

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
                ALTER TABLE users
                ADD COLUMN default_reminder_time TEXT NOT NULL DEFAULT '09:00'
                CHECK (
                    length(default_reminder_time) = 5
                    AND default_reminder_time GLOB '[0-2][0-9]:[0-5][0-9]'
                    AND substr(default_reminder_time, 1, 2) BETWEEN '00' AND '23'
                )
                """
            )
            connection.execute(
                """
                ALTER TABLE personal_items
                ADD COLUMN reminder_prompt_dismissed_at TEXT
                """
            )
            _execute_statements(connection, USER_SESSIONS_OWNERSHIP_INDEX_SQL)
            connection.execute(REMINDERS_TABLE_SQL)
            connection.execute(PUSH_SUBSCRIPTIONS_TABLE_SQL)
            connection.execute(REMINDER_DELIVERIES_TABLE_SQL)
            _execute_statements(connection, V4_INDEXES_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            _validate_migrated_data(connection, preflight)
            Database._validate_v4_schema(connection, _table_names(connection))

            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
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
    if version != SCHEMA_V3_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V3_VERSION}, got {version}"
        )

    tables = _table_names(connection)
    try:
        Database._validate_v3_schema(connection, tables)
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc

    unexpected_tables = {
        "reminders",
        "push_subscriptions",
        "reminder_deliveries",
    } & tables
    if unexpected_tables:
        names = ", ".join(sorted(unexpected_tables))
        raise MigrationError(f"version 3 database contains v4 tables: {names}")

    for row in connection.execute("SELECT id, timezone FROM users ORDER BY id"):
        try:
            validate_timezone_name(row["timezone"])
        except ValueError as exc:
            raise MigrationError(
                f"user id {row['id']} has an invalid IANA timezone"
            ) from exc

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
    }
    for table_name, expected_count in expected_counts.items():
        if _row_count(connection, table_name) != expected_count:
            raise MigrationError(f"{table_name} row count changed during migration")

    if connection.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE default_reminder_time != '09:00'
        """
    ).fetchone()[0]:
        raise MigrationError("default reminder time backfill failed")


def _preflight_from_connection(
    database_path: Path,
    connection: sqlite3.Connection,
) -> MigrationPreflight:
    return MigrationPreflight(
        database_path=database_path,
        schema_version=SCHEMA_V3_VERSION,
        user_count=_row_count(connection, "users"),
        personal_item_count=_row_count(connection, "personal_items"),
        item_input_count=_row_count(connection, "item_inputs"),
        user_session_count=_row_count(connection, "user_sessions"),
    )


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def _execute_statements(connection: sqlite3.Connection, sql: str) -> None:
    for statement in sql.split(";"):
        if statement.strip():
            connection.execute(statement)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly migrate a stopped SelfEcho schema v3 database to v4."
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Path to the stopped schema v3 SQLite database",
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
    parser = _build_argument_parser()
    arguments = parser.parse_args(argv)

    try:
        preflight = inspect_v3_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        output_fn(f"Users: {preflight.user_count}")
        output_fn(f"Personal Items: {preflight.personal_item_count}")
        output_fn(f"Item Inputs: {preflight.item_input_count}")
        output_fn(f"User Sessions: {preflight.user_session_count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0

        output_fn("A validated, recoverable schema v3 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE V3 TO V4" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE V3 TO V4":
            output_fn("Migration cancelled. No database changes were made.")
            return 2

        result = migrate_v3_to_v4(preflight.database_path)
        output_fn("Migration completed successfully.")
        output_fn(f"Users preserved: {result.user_count}")
        output_fn(f"Personal Items preserved: {result.personal_item_count}")
        output_fn(f"Item Inputs preserved: {result.item_input_count}")
        output_fn(f"User Sessions preserved: {result.user_session_count}")
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
