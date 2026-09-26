"""Explicit v8 to v9 Voice Segment state migration for a stopped database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    Database,
    REQUIRED_V8_COLUMNS,
    SCHEMA_V8_VERSION,
    SCHEMA_V9_VERSION,
    V5_INDEXES_SQL,
    VOICE_SEGMENTS_V9_TABLE_SQL,
)


EXISTING_TABLES = tuple(REQUIRED_V8_COLUMNS)

# The v8 Voice Segment index set this migration accepts as its source. A
# different set means the database does not match the published history.
VOICE_SEGMENT_INDEXES = frozenset({
    "uq_voice_segments_user_client",
    "uq_voice_segments_draft_position",
    "uq_voice_segments_input_position",
    "uq_voice_segments_draft_active",
    "idx_voice_segments_user_draft",
    "idx_voice_segments_user_input",
})


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    voice_segment_count: int
    row_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    schema_version: int
    voice_segment_count: int
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


def _voice_segment_indexes(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='voice_segments' AND sql IS NOT NULL"
        )
    }


def _validate_source_database(connection: sqlite3.Connection) -> int:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V8_VERSION:
        raise MigrationError(
            f"expected database schema version {SCHEMA_V8_VERSION}, got {version}"
        )
    try:
        Database._validate_v8_schema(connection, _table_names(connection))
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc
    if _voice_segment_indexes(connection) != set(VOICE_SEGMENT_INDEXES):
        raise MigrationError(
            "unexpected Voice Segment indexes; migration refused"
        )
    _validate_integrity(connection)
    return int(connection.execute("SELECT COUNT(*) FROM voice_segments").fetchone()[0])


def inspect_v8_database(database_path: Path) -> MigrationPreflight:
    """Validate a v8 database through a strictly read-only connection."""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path, read_only=True)
        connection.execute("BEGIN")
        voice_segment_count = _validate_source_database(connection)
        return MigrationPreflight(
            database_path=database_path.expanduser().resolve(),
            schema_version=SCHEMA_V8_VERSION,
            voice_segment_count=voice_segment_count,
            row_counts=_row_counts(connection),
        )
    except MigrationError:
        raise
    except (sqlite3.DatabaseError, RuntimeError) as exc:
        raise MigrationError(f"schema v8 preflight failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _apply_schema_changes(connection: sqlite3.Connection) -> None:
    # The old table has no inbound foreign keys. SQLite DDL remains transactional.
    sequence_row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='voice_segments'"
    ).fetchone()
    previous_sequence = sequence_row[0] if sequence_row else 0
    connection.execute("ALTER TABLE voice_segments RENAME TO voice_segments_v8")
    connection.execute(VOICE_SEGMENTS_V9_TABLE_SQL)
    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(voice_segments_v8)")
    ]
    names = ", ".join(f'"{name}"' for name in columns)
    connection.execute(
        f"INSERT INTO voice_segments ({names}) SELECT {names} FROM voice_segments_v8"
    )
    connection.execute("DROP TABLE voice_segments_v8")
    current_row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='voice_segments'"
    ).fetchone()
    current_sequence = current_row[0] if current_row else 0
    if previous_sequence > current_sequence:
        cursor = connection.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'voice_segments'",
            (previous_sequence,),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('voice_segments', ?)",
                (previous_sequence,),
            )
    for statement in V5_INDEXES_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _voice_segment_snapshot(connection: sqlite3.Connection) -> list[tuple]:
    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(voice_segments)")
    ]
    names = ", ".join(f'"{name}"' for name in columns)
    return [
        tuple(row)
        for row in connection.execute(
            f"SELECT {names} FROM voice_segments ORDER BY id"
        )
    ]

def _validate_migrated_data(
    connection: sqlite3.Connection,
    before: dict[str, int],
    voice_segments_before: list[tuple],
) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_V9_VERSION:
        raise MigrationError("migration did not produce schema version 9")
    try:
        Database._validate_v9_schema(connection, _table_names(connection))
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc
    if _row_counts(connection) != before:
        raise MigrationError("existing table row counts changed during migration")
    if _voice_segment_snapshot(connection) != voice_segments_before:
        raise MigrationError("Voice Segment data changed during migration")
    _validate_integrity(connection)


def migrate_v8_to_v9(database_path: Path) -> MigrationResult:
    """Explicitly migrate a stopped, backed-up v8 database to schema v9."""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path, read_only=False)
        connection.execute("BEGIN IMMEDIATE")
        # Recheck under the write lock; an earlier CLI preflight is not authority.
        voice_segment_count = _validate_source_database(connection)
        before = _row_counts(connection)
        voice_segments_before = _voice_segment_snapshot(connection)
        _apply_schema_changes(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_V9_VERSION}")
        _validate_migrated_data(connection, before, voice_segments_before)
        connection.commit()
        return MigrationResult(
            schema_version=SCHEMA_V9_VERSION,
            voice_segment_count=voice_segment_count,
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
        description="Explicitly migrate a stopped SelfEcho schema v8 database to v9."
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
        preflight = inspect_v8_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        output_fn(f"Voice Segments: {preflight.voice_segment_count}")
        for table_name, count in preflight.row_counts.items():
            output_fn(f"{table_name}: {count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0
        output_fn("Stop application writes. A validated, recoverable schema v8 backup is required.")
        confirmation = input_fn(
            'Type "MIGRATE PUBLIC V8 TO V9" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE PUBLIC V8 TO V9":
            output_fn("Migration cancelled. No database changes were made.")
            return 2
        result = migrate_v8_to_v9(preflight.database_path)
        output_fn("Migration completed successfully.")
        output_fn(f"Voice Segments preserved: {result.voice_segment_count}")
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
