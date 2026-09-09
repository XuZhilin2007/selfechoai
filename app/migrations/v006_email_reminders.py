from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    CURRENT_SCHEMA_VERSION,
    EMAIL_REMINDER_SETTINGS_TABLE_SQL,
    EMAIL_VERIFICATION_CHALLENGES_TABLE_SQL,
    REMINDER_EMAIL_DELIVERIES_TABLE_SQL,
    SCHEMA_V5_VERSION,
    V6_INDEXES_SQL,
    Database,
)


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
)
NEW_TABLES = (
    "email_reminder_settings",
    "email_verification_challenges",
    "reminder_email_deliveries",
)
NEW_INDEXES = (
    "idx_email_challenges_user_address_created",
    "uq_email_challenges_active",
    "idx_email_deliveries_send_ready",
    "idx_email_deliveries_status_ready",
    "idx_email_deliveries_user_status",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    row_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    row_counts: dict[str, int]


def inspect_v5_database(database_path: Path) -> MigrationPreflight:
    """Validate a v5 database using a strictly read-only connection."""

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
        return MigrationPreflight(
            database_path=resolved,
            schema_version=SCHEMA_V5_VERSION,
            row_counts={name: _row_count(connection, name) for name in EXISTING_TABLES},
        )
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite preflight failed: {exc}") from exc
    finally:
        connection.close()


def migrate_v5_to_v6(database_path: Path) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up v5 database to schema v6."""

    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise MigrationError(f"database file does not exist: {resolved}")
    try:
        connection = sqlite3.connect(resolved, timeout=10)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database for migration: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _validate_source_database(connection)
        preflight = MigrationPreflight(
            database_path=resolved,
            schema_version=SCHEMA_V5_VERSION,
            row_counts={name: _row_count(connection, name) for name in EXISTING_TABLES},
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(EMAIL_REMINDER_SETTINGS_TABLE_SQL)
            connection.execute(EMAIL_VERIFICATION_CHALLENGES_TABLE_SQL)
            connection.execute(REMINDER_EMAIL_DELIVERIES_TABLE_SQL)
            _execute_statements(connection, V6_INDEXES_SQL)
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            _validate_migrated_data(connection, preflight)
            Database._validate_v6_schema(connection, _table_names(connection))
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
        return MigrationResult(row_counts=dict(preflight.row_counts))
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
    if version != SCHEMA_V5_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V5_VERSION}, got {version}"
        )
    tables = _table_names(connection)
    try:
        Database._validate_v5_schema(connection, tables)
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc
    unexpected = set(NEW_TABLES) & tables
    if unexpected:
        raise MigrationError(
            "version 5 database contains v6 tables: "
            + ", ".join(sorted(unexpected))
        )
    unexpected_indexes = set(NEW_INDEXES) & _index_names(connection)
    if unexpected_indexes:
        raise MigrationError(
            "version 5 database contains v6 indexes: "
            + ", ".join(sorted(unexpected_indexes))
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"source database integrity check failed: {integrity}")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("source database foreign key validation failed")


def _validate_migrated_data(
    connection: sqlite3.Connection,
    preflight: MigrationPreflight,
) -> None:
    for table_name, expected_count in preflight.row_counts.items():
        if _row_count(connection, table_name) != expected_count:
            raise MigrationError(f"{table_name} row count changed during migration")
    for table_name in NEW_TABLES:
        if _row_count(connection, table_name) != 0:
            raise MigrationError(f"new table {table_name} was not empty")


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
        description=(
            "Explicitly migrate a stopped SelfEcho Community Edition "
            "schema v5 database to v6."
        )
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
        preflight = inspect_v5_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        for table_name, count in preflight.row_counts.items():
            output_fn(f"{table_name}: {count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0
        output_fn("A validated, recoverable schema v5 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE PUBLIC V5 TO V6" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE PUBLIC V5 TO V6":
            output_fn("Migration cancelled. No database changes were made.")
            return 2
        result = migrate_v5_to_v6(preflight.database_path)
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
