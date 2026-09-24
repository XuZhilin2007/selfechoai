from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    Database,
    REQUIRED_V7_COLUMNS,
    SCHEMA_V7_VERSION,
    SCHEMA_V8_VERSION,
    V8_PIN_COLUMN_SQL,
)


EXISTING_TABLES = tuple(REQUIRED_V7_COLUMNS)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    row_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    schema_version: int
    row_counts: dict[str, int]


def _connect(database_path: Path, *, read_only: bool) -> sqlite3.Connection:
    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise MigrationError(f"database file does not exist: {resolved}")
    # Explicit rw mode, unlike the default, cannot create a missing database.
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode={'ro' if read_only else 'rw'}",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in EXISTING_TABLES
    }


def _validate_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("database foreign key validation failed")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"database integrity check failed: {integrity}")


def _validate_source_database(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V7_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V7_VERSION}, got {version}"
        )
    try:
        Database._validate_v7_schema(connection, _table_names(connection))
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(personal_items)")
    }
    if "is_pinned" in columns:
        raise MigrationError("version 7 database already contains is_pinned")
    _validate_integrity(connection)


def inspect_v7_database(database_path: Path) -> MigrationPreflight:
    """Validate a v7 database through a strictly read-only connection."""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path, read_only=True)
        connection.execute("BEGIN")
        _validate_source_database(connection)
        return MigrationPreflight(
            database_path=database_path.expanduser().resolve(),
            schema_version=SCHEMA_V7_VERSION,
            row_counts=_row_counts(connection),
        )
    except MigrationError:
        raise
    except (sqlite3.DatabaseError, RuntimeError) as exc:
        raise MigrationError(f"schema v7 preflight failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _apply_schema_changes(connection: sqlite3.Connection) -> None:
    connection.execute(V8_PIN_COLUMN_SQL)


def _validate_migrated_data(
    connection: sqlite3.Connection,
    before: dict[str, int],
) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V8_VERSION:
        raise MigrationError("migration did not produce schema version 8")
    Database._validate_v8_schema(connection, _table_names(connection))
    if _row_counts(connection) != before:
        raise MigrationError("existing table row counts changed during migration")
    unpinned_violations = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM personal_items
            WHERE is_pinned IS NULL OR is_pinned != 0
            """
        ).fetchone()[0]
    )
    if unpinned_violations:
        raise MigrationError("historical items must all be unpinned")
    _validate_integrity(connection)


def migrate_v7_to_v8(database_path: Path) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up v7 database to schema v8."""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path, read_only=False)
        connection.execute("BEGIN IMMEDIATE")
        # Recheck under the write lock; an earlier CLI preflight is not authority.
        _validate_source_database(connection)
        before = _row_counts(connection)
        _apply_schema_changes(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_V8_VERSION}")
        _validate_migrated_data(connection, before)
        connection.commit()
        return MigrationResult(
            schema_version=SCHEMA_V8_VERSION,
            row_counts=before,
        )
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"migration aborted and transaction was rolled back: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly migrate a stopped SelfEcho schema v7 database to v8."
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
        preflight = inspect_v7_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        for table_name, count in preflight.row_counts.items():
            output_fn(f"{table_name}: {count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0
        output_fn("Stop application writes. A validated, recoverable schema v7 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE PUBLIC V7 TO V8" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE PUBLIC V7 TO V8":
            output_fn("Migration cancelled. No database changes were made.")
            return 2
        result = migrate_v7_to_v8(preflight.database_path)
        output_fn("Migration completed successfully.")
        for table_name, count in result.row_counts.items():
            output_fn(f"{table_name} preserved: {count}")
        output_fn(f"Schema version is now {result.schema_version}.")
        return 0
    except (MigrationError, ValueError) as exc:
        output_fn(f"ERROR: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        output_fn("Migration cancelled. No database changes were requested.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
