from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import (
    CURRENT_SCHEMA_VERSION,
    Database,
    REQUIRED_V6_INDEXES,
)
from app.repository import Repository
from app.schemas import InputMethod


NOW = "2026-09-07T01:00:00+00:00"


def insert_user(connection: sqlite3.Connection, user_id: int) -> None:
    connection.execute(
        """
        INSERT INTO users (
            id, email, password_hash, display_name, timezone, status,
            password_changed_time, created_time, updated_time
        ) VALUES (?, ?, 'hash', ?, 'UTC', 'active', ?, ?, ?)
        """,
        (user_id, f"user-{user_id}@example.com", f"User {user_id}", NOW, NOW, NOW),
    )


def create_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def test_fresh_database_initializes_complete_public_schema_v6(tmp_path: Path):
    database = create_database(tmp_path / "fresh-v6.db")
    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        voice_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("capture_drafts", "voice_segments", "voice_file_deletions")
        }
        item_input_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(item_inputs)")
        }

    assert version == CURRENT_SCHEMA_VERSION == 6
    assert {
        "users",
        "user_sessions",
        "personal_items",
        "item_inputs",
        "reminders",
        "push_subscriptions",
        "reminder_deliveries",
        "capture_drafts",
        "voice_segments",
        "voice_file_deletions",
        "email_reminder_settings",
        "email_verification_challenges",
        "reminder_email_deliveries",
    } <= tables
    assert REQUIRED_V6_INDEXES <= indexes
    assert "source_draft_id" in item_input_columns
    assert voice_counts == {
        "capture_drafts": 0,
        "voice_segments": 0,
        "voice_file_deletions": 0,
    }
    with database.connection() as connection:
        email_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "email_reminder_settings",
                "email_verification_challenges",
                "reminder_email_deliveries",
            )
        }
    assert email_counts == {
        "email_reminder_settings": 0,
        "email_verification_challenges": 0,
        "reminder_email_deliveries": 0,
    }


def test_fresh_v5_database_preserves_normal_non_voice_operation(tmp_path: Path):
    database = create_database(tmp_path / "non-voice.db")
    with database.transaction() as connection:
        insert_user(connection, 1)

    item_input = Repository(database).create_input(
        "原始文本仍可正常保存",
        InputMethod.TEXT,
        user_id=1,
    )

    assert item_input.original_text == "原始文本仍可正常保存"
    assert item_input.input_method == InputMethod.TEXT


def test_draft_and_source_idempotency_constraints(tmp_path: Path):
    database = create_database(tmp_path / "draft-constraints.db")
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA foreign_keys = ON")
    insert_user(connection, 1)
    insert_user(connection, 2)
    connection.execute(
        """
        INSERT INTO capture_drafts (
            id, user_id, current_text, revision, created_time, updated_time
        ) VALUES (10, 1, ?, 0, ?, ?)
        """,
        ("x" * 10_000, NOW, NOW),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO capture_drafts (
                user_id, current_text, revision, created_time, updated_time
            ) VALUES (1, 'duplicate', 0, ?, ?)
            """,
            (NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO capture_drafts (
                user_id, current_text, revision, created_time, updated_time
            ) VALUES (2, ?, 0, ?, ?)
            """,
            ("x" * 10_001, NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE capture_drafts SET revision = -1 WHERE id = 10"
        )

    connection.execute(
        """
        INSERT INTO item_inputs (
            user_id, source_draft_id, original_text, input_method,
            processing_status, created_time
        ) VALUES (1, 10, 'first', 'text', 'pending', ?)
        """,
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO item_inputs (
                user_id, source_draft_id, original_text, input_method,
                processing_status, created_time
            ) VALUES (1, 10, 'duplicate', 'text', 'pending', ?)
            """,
            (NOW,),
        )
    connection.execute(
        """
        INSERT INTO item_inputs (
            user_id, source_draft_id, original_text, input_method,
            processing_status, created_time
        ) VALUES (2, 10, 'independent user', 'text', 'pending', ?)
        """,
        (NOW,),
    )
    connection.close()


def test_voice_segment_parent_ownership_and_one_active_constraints(tmp_path: Path):
    database = create_database(tmp_path / "segment-constraints.db")
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA foreign_keys = ON")
    insert_user(connection, 1)
    insert_user(connection, 2)
    for draft_id, user_id in ((10, 1), (20, 2)):
        connection.execute(
            """
            INSERT INTO capture_drafts (
                id, user_id, current_text, revision, created_time, updated_time
            ) VALUES (?, ?, '', 0, ?, ?)
            """,
            (draft_id, user_id, NOW, NOW),
        )
    connection.execute(
        """
        INSERT INTO voice_segments (
            user_id, draft_id, item_input_id, position, client_segment_id,
            storage_key, original_size_bytes, original_sha256,
            transcription_status, provider, model, created_time, updated_time
        ) VALUES (1, 10, NULL, 0, 'client-1', 'original/aa/one.bin', 1, ?,
                  'pending', 'alibaba', 'qwen-audio-3.0-asr-flash', ?, ?)
        """,
        ("a" * 64, NOW, NOW),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, draft_id, position, client_segment_id, storage_key,
                original_size_bytes, original_sha256, transcription_status,
                provider, model, created_time, updated_time
            ) VALUES (1, 10, 1, 'client-2', 'original/aa/two.bin', 1, ?,
                      'pending', 'alibaba', 'qwen-audio-3.0-asr-flash', ?, ?)
            """,
            ("b" * 64, NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, draft_id, position, client_segment_id, storage_key,
                original_size_bytes, original_sha256, transcription_status,
                provider, model, failure_code, failure_message,
                transcription_finished_time, created_time, updated_time
            ) VALUES (1, 20, 0, 'wrong-owner', 'original/aa/owner.bin', 1, ?,
                      'failed', 'alibaba', 'qwen-audio-3.0-asr-flash',
                      'internal', 'failed', ?, ?, ?)
            """,
            ("c" * 64, NOW, NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, draft_id, item_input_id, position, client_segment_id,
                storage_key, original_size_bytes, original_sha256,
                transcription_status, provider, model, created_time, updated_time
            ) VALUES (1, NULL, NULL, 2, 'no-parent', 'original/aa/none.bin', 1, ?,
                      'pending', 'alibaba', 'qwen-audio-3.0-asr-flash', ?, ?)
            """,
            ("d" * 64, NOW, NOW),
        )

    connection.execute(
        """
        INSERT INTO item_inputs (
            id, user_id, original_text, input_method, processing_status, created_time
        ) VALUES (30, 1, 'saved', 'voice', 'pending', ?)
        """,
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, draft_id, item_input_id, position, client_segment_id,
                storage_key, original_size_bytes, original_sha256,
                transcription_status, provider, model, created_time, updated_time
            ) VALUES (1, 10, 30, 3, 'both-parent', 'original/aa/both.bin', 1, ?,
                      'pending', 'alibaba', 'qwen-audio-3.0-asr-flash', ?, ?)
            """,
            ("e" * 64, NOW, NOW),
        )
    connection.execute(
        """
        INSERT INTO voice_segments (
            user_id, draft_id, item_input_id, position, client_segment_id,
            storage_key, original_size_bytes, original_sha256,
            transcription_status, provider, model, provider_transcript,
            provider_request_id, transcription_finished_time,
            created_time, updated_time
        ) VALUES (1, NULL, 30, 0, 'saved-segment', 'original/aa/saved.bin', 1, ?,
                  'succeeded', 'alibaba', 'qwen-audio-3.0-asr-flash', 'text',
                  'request', ?, ?, ?)
        """,
        ("f" * 64, NOW, NOW, NOW),
    )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


@pytest.mark.parametrize(
    "values",
    [
        {
            "status": "pending",
            "transcript": None,
            "failure_code": "internal",
            "failure_message": "bad",
            "started": None,
            "finished": None,
        },
        {
            "status": "transcribing",
            "transcript": None,
            "failure_code": None,
            "failure_message": None,
            "started": None,
            "finished": None,
        },
        {
            "status": "succeeded",
            "transcript": "   ",
            "failure_code": None,
            "failure_message": None,
            "started": NOW,
            "finished": NOW,
        },
        {
            "status": "failed",
            "transcript": None,
            "failure_code": "network",
            "failure_message": None,
            "started": NOW,
            "finished": NOW,
        },
    ],
)
def test_voice_segment_state_constraints_reject_invalid_combinations(
    tmp_path: Path,
    values: dict[str, str | None],
):
    database = create_database(tmp_path / f"invalid-{values['status']}.db")
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA foreign_keys = ON")
    insert_user(connection, 1)
    connection.execute(
        """
        INSERT INTO item_inputs (
            id, user_id, original_text, input_method, processing_status, created_time
        ) VALUES (30, 1, 'saved', 'voice', 'pending', ?)
        """,
        (NOW,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, item_input_id, position, client_segment_id, storage_key,
                original_size_bytes, original_sha256, transcription_status,
                provider, model, provider_transcript, failure_code,
                failure_message, transcription_started_time,
                transcription_finished_time, created_time, updated_time
            ) VALUES (1, 30, 0, 'invalid-state', 'original/aa/invalid.bin',
                      1, ?, ?, 'alibaba', 'qwen-audio-3.0-asr-flash',
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 64,
                values["status"],
                values["transcript"],
                values["failure_code"],
                values["failure_message"],
                values["started"],
                values["finished"],
                NOW,
                NOW,
            ),
        )
    connection.close()


def test_deletion_reason_and_voice_identity_constraints(tmp_path: Path):
    database = create_database(tmp_path / "identity-constraints.db")
    connection = sqlite3.connect(database.path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO voice_file_deletions (storage_key, reason, created_time)
            VALUES ('original/aa/file.bin', 'unknown_reason', ?)
            """,
            (NOW,),
        )
    connection.close()
