from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.auth import hash_password, normalize_email
from app.database import (
    ITEM_INPUTS_TABLE_SQL,
    PERSONAL_ITEMS_TABLE_SQL,
    SCHEMA_VERSION,
    USERS_TABLE_SQL,
    USER_SESSIONS_TABLE_SQL,
    V3_INDEXES_SQL,
)


SOURCE_SCHEMA_VERSION = 2

V2_REQUIRED_COLUMNS = {
    "personal_items": {
        "id",
        "title",
        "type",
        "importance",
        "urgency",
        "deadline",
        "estimated_time",
        "status",
        "next_action",
        "extra_information",
        "created_time",
        "updated_time",
    },
    "item_inputs": {
        "id",
        "item_id",
        "original_text",
        "input_method",
        "processing_status",
        "failure_type",
        "failure_message",
        "created_time",
    },
}

PERSONAL_ITEM_COPY_COLUMNS = (
    "id",
    "title",
    "type",
    "importance",
    "urgency",
    "deadline",
    "estimated_time",
    "status",
    "next_action",
    "extra_information",
    "created_time",
    "updated_time",
)

ITEM_INPUT_COPY_COLUMNS = (
    "id",
    "item_id",
    "original_text",
    "input_method",
    "processing_status",
    "failure_type",
    "failure_message",
    "created_time",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerUser:
    email: str
    password_hash: str
    display_name: str
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    owner_user_id: int
    personal_item_count: int
    item_input_count: int


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    database_path: Path
    schema_version: int
    personal_item_count: int
    item_input_count: int


def inspect_v2_database(database_path: Path) -> MigrationPreflight:
    """Run read-only v2 validation before prompting for Owner credentials."""

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
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return MigrationPreflight(
            database_path=database_path,
            schema_version=version,
            personal_item_count=_row_count(connection, "personal_items"),
            item_input_count=_row_count(connection, "item_inputs"),
        )
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite preflight failed: {exc}") from exc
    finally:
        connection.close()


def migrate_v2_to_v3(database_path: Path, owner: OwnerUser) -> MigrationResult:
    """Explicitly migrate a stopped v2 database and assign all data to Owner.

    The caller must create a recoverable backup first and provide an already
    hashed Owner password. The CLI below performs the interactive password
    bootstrap without putting credentials in process arguments.
    """

    database_path = database_path.expanduser().resolve()
    if not database_path.is_file():
        raise MigrationError(f"database file does not exist: {database_path}")

    email = normalize_email(owner.email)
    password_hash = owner.password_hash.strip()
    display_name = owner.display_name.strip()
    timezone_name = owner.timezone.strip()
    for field_name, value in (
        ("email", email),
        ("password_hash", password_hash),
        ("display_name", display_name),
        ("timezone", timezone_name),
    ):
        if not value:
            raise MigrationError(f"owner {field_name} must not be blank")

    try:
        connection = sqlite3.connect(database_path, timeout=10)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"cannot open database for migration: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = OFF")

    try:
        _validate_source_database(connection)
        personal_item_count = _row_count(connection, "personal_items")
        item_input_count = _row_count(connection, "item_inputs")
        now = datetime.now(timezone.utc).isoformat()

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "ALTER TABLE item_inputs RENAME TO _v2_item_inputs"
            )
            connection.execute(
                "ALTER TABLE personal_items RENAME TO _v2_personal_items"
            )

            connection.execute(USERS_TABLE_SQL)
            connection.execute(PERSONAL_ITEMS_TABLE_SQL)
            connection.execute(ITEM_INPUTS_TABLE_SQL)
            connection.execute(USER_SESSIONS_TABLE_SQL)

            owner_cursor = connection.execute(
                """
                INSERT INTO users (
                    email, password_hash, display_name, timezone, status,
                    password_changed_time, created_time, updated_time
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    email,
                    password_hash,
                    display_name,
                    timezone_name,
                    now,
                    now,
                    now,
                ),
            )
            owner_user_id = int(owner_cursor.lastrowid)

            personal_columns = ", ".join(PERSONAL_ITEM_COPY_COLUMNS[1:])
            connection.execute(
                f"""
                INSERT INTO personal_items (id, user_id, {personal_columns})
                SELECT id, ?, {personal_columns}
                FROM _v2_personal_items
                """,
                (owner_user_id,),
            )

            input_columns = ", ".join(ITEM_INPUT_COPY_COLUMNS[1:])
            connection.execute(
                f"""
                INSERT INTO item_inputs (id, user_id, {input_columns})
                SELECT id, ?, {input_columns}
                FROM _v2_item_inputs
                """,
                (owner_user_id,),
            )

            _validate_copied_data(
                connection,
                owner_user_id=owner_user_id,
                personal_item_count=personal_item_count,
                item_input_count=item_input_count,
            )

            connection.execute("DROP TABLE _v2_item_inputs")
            connection.execute("DROP TABLE _v2_personal_items")
            _execute_statements(connection, V3_INDEXES_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise MigrationError("foreign key validation failed after migration")

            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MigrationError(f"database integrity check failed: {integrity}")

            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return MigrationResult(
            owner_user_id=owner_user_id,
            personal_item_count=personal_item_count,
            item_input_count=item_input_count,
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
    if version != SOURCE_SCHEMA_VERSION:
        raise MigrationError(
            f"expected database schema version {SOURCE_SCHEMA_VERSION}, got {version}"
        )

    tables = {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    missing_tables = set(V2_REQUIRED_COLUMNS) - tables
    if missing_tables:
        names = ", ".join(sorted(missing_tables))
        raise MigrationError(f"version 2 database is missing tables: {names}")

    if {"users", "user_sessions"} & tables:
        raise MigrationError("version 2 database already contains authentication tables")

    for table_name, required_columns in V2_REQUIRED_COLUMNS.items():
        actual_columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        missing_columns = required_columns - actual_columns
        if missing_columns:
            names = ", ".join(sorted(missing_columns))
            raise MigrationError(
                f"version 2 table {table_name} is missing columns: {names}"
            )

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"source database integrity check failed: {integrity}")

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise MigrationError("source database foreign key validation failed")


def _validate_copied_data(
    connection: sqlite3.Connection,
    *,
    owner_user_id: int,
    personal_item_count: int,
    item_input_count: int,
) -> None:
    if _row_count(connection, "personal_items") != personal_item_count:
        raise MigrationError("personal item count changed during migration")
    if _row_count(connection, "item_inputs") != item_input_count:
        raise MigrationError("item input count changed during migration")

    if connection.execute(
        "SELECT COUNT(*) FROM personal_items WHERE user_id != ?",
        (owner_user_id,),
    ).fetchone()[0]:
        raise MigrationError("not all personal items were assigned to Owner")
    if connection.execute(
        "SELECT COUNT(*) FROM item_inputs WHERE user_id != ?",
        (owner_user_id,),
    ).fetchone()[0]:
        raise MigrationError("not all item inputs were assigned to Owner")

    _assert_rows_preserved(
        connection,
        old_table="_v2_personal_items",
        new_table="personal_items",
        columns=PERSONAL_ITEM_COPY_COLUMNS,
    )
    _assert_rows_preserved(
        connection,
        old_table="_v2_item_inputs",
        new_table="item_inputs",
        columns=ITEM_INPUT_COPY_COLUMNS,
    )


def _assert_rows_preserved(
    connection: sqlite3.Connection,
    *,
    old_table: str,
    new_table: str,
    columns: tuple[str, ...],
) -> None:
    selected_columns = ", ".join(columns)
    for left_table, right_table in (
        (old_table, new_table),
        (new_table, old_table),
    ):
        difference_count = connection.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT {selected_columns} FROM {left_table}
                EXCEPT
                SELECT {selected_columns} FROM {right_table}
            )
            """
        ).fetchone()[0]
        if difference_count:
            raise MigrationError(f"rows changed while copying {new_table}")


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly migrate a stopped SelfEcho AI v2 SQLite database to v3. "
            "Owner credentials are collected interactively."
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="path to the stopped v2 SQLite database",
    )
    parser.add_argument(
        "--owner-email",
        help="Owner email; prompted when omitted",
    )
    parser.add_argument(
        "--owner-display-name",
        help="Owner display name; prompted when omitted",
    )
    parser.add_argument(
        "--owner-timezone",
        help="Owner timezone; prompted with Asia/Shanghai as the default",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the source database without changing it",
    )
    return parser


def _required_prompt(
    current_value: str | None,
    prompt: str,
    input_fn: Callable[[str], str],
) -> str:
    value = current_value if current_value is not None else input_fn(prompt)
    value = value.strip()
    if not value:
        raise MigrationError(f"{prompt.rstrip(': ')} must not be blank")
    return value


def _prompt_owner(
    arguments: argparse.Namespace,
    *,
    input_fn: Callable[[str], str],
    password_fn: Callable[[str], str],
) -> OwnerUser:
    email = normalize_email(
        _required_prompt(arguments.owner_email, "Owner email: ", input_fn)
    )
    if "@" not in email:
        raise MigrationError("Owner email must contain @")
    display_name = _required_prompt(
        arguments.owner_display_name,
        "Owner display name: ",
        input_fn,
    )
    if arguments.owner_timezone is None:
        timezone_name = input_fn("Owner timezone [Asia/Shanghai]: ").strip()
        timezone_name = timezone_name or "Asia/Shanghai"
    else:
        timezone_name = arguments.owner_timezone.strip()
    if not timezone_name:
        raise MigrationError("Owner timezone must not be blank")

    password = password_fn("Owner password (hidden, 12-128 characters): ")
    password_confirmation = password_fn("Confirm Owner password: ")
    if password != password_confirmation:
        raise MigrationError("Owner passwords do not match")
    if not 12 <= len(password) <= 128:
        raise MigrationError("Owner password must be 12-128 characters")

    return OwnerUser(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        timezone=timezone_name,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
    output_fn: Callable[[str], None] = print,
) -> int:
    parser = _build_argument_parser()
    arguments = parser.parse_args(argv)

    try:
        preflight = inspect_v2_database(arguments.database)
        output_fn(f"Database: {preflight.database_path}")
        output_fn(f"Schema version: {preflight.schema_version}")
        output_fn(f"Personal Items: {preflight.personal_item_count}")
        output_fn(f"Item Inputs: {preflight.item_input_count}")
        if arguments.check_only:
            output_fn("Preflight passed. No database changes were made.")
            return 0

        owner = _prompt_owner(
            arguments,
            input_fn=input_fn,
            password_fn=password_fn,
        )
        output_fn(f"Owner email: {owner.email}")
        output_fn(f"Owner display name: {owner.display_name}")
        output_fn("A recoverable backup is required before continuing.")
        confirmation = input_fn(
            'Type "MIGRATE V2 TO V3" to execute the transaction: '
        ).strip()
        if confirmation != "MIGRATE V2 TO V3":
            output_fn("Migration cancelled. No database changes were made.")
            return 2

        result = migrate_v2_to_v3(preflight.database_path, owner)
        output_fn("Migration completed successfully.")
        output_fn(f"Owner user id: {result.owner_user_id}")
        output_fn(f"Personal Items preserved: {result.personal_item_count}")
        output_fn(f"Item Inputs preserved: {result.item_input_count}")
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
