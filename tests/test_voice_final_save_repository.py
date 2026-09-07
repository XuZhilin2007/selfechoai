from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import Database
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.voice_repository import (
    DraftBlockedError,
    DraftRevisionConflictError,
    VoiceCaptureRepository,
)


NOW = "2026-09-07T01:00:00+00:00"


def make_repository(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    with database.transaction() as connection:
        for user_id in (1, 2):
            connection.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, display_name, timezone, status,
                    password_changed_time, created_time, updated_time
                ) VALUES (?, ?, 'hash', ?, 'UTC', 'active', ?, ?, ?)
                """,
                (
                    user_id,
                    f"user-{user_id}@example.com",
                    f"User {user_id}",
                    NOW,
                    NOW,
                    NOW,
                ),
            )
    return database, VoiceCaptureRepository(database), Repository(database)


def insert_segment(
    database: Database,
    *,
    draft_id: int,
    status: str,
    suffix: str = "one",
    position: int = 0,
) -> int:
    state = {
        "pending": (None, None, None, None, None),
        "transcribing": (None, None, None, NOW, None),
        "failed": (None, "network", "safe failure", NOW, NOW),
        "succeeded": ("machine transcript", None, None, NOW, NOW),
    }[status]
    transcript, failure_code, failure_message, started, finished = state
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, draft_id, position, client_segment_id, storage_key,
                original_size_bytes, original_sha256, transcription_status,
                provider, model, provider_transcript, provider_request_id,
                failure_code, failure_message, created_time, updated_time,
                transcription_started_time, transcription_finished_time
            ) VALUES (
                1, ?, ?, ?, ?, 5, ?, ?, 'alibaba',
                'qwen-audio-3.0-asr-flash', ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                draft_id,
                position,
                f"client-{suffix}",
                f"original/aa/{suffix}.bin",
                suffix[0] * 64,
                status,
                transcript,
                f"request-{suffix}" if transcript else None,
                failure_code,
                failure_message,
                NOW,
                NOW,
                started,
                finished,
            ),
        )
        return int(cursor.lastrowid)


def test_final_save_atomically_preserves_exact_text_and_reparents_segments(
    tmp_path: Path,
):
    database, voice_repository, repository = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "  用户最终确认文本\n", 0)
    segment_ids = [
        insert_segment(
            database,
            draft_id=draft.id,
            status="succeeded",
            suffix="one",
            position=0,
        ),
        insert_segment(
            database,
            draft_id=draft.id,
            status="succeeded",
            suffix="two",
            position=1,
        ),
    ]

    result = voice_repository.save_draft(1, draft.id, draft.revision)

    assert result.created is True
    assert result.item_input.original_text == "  用户最终确认文本\n"
    assert result.item_input.input_method.value == "voice"
    assert result.item_input.processing_status.value == "pending"
    assert result.item_input.voice_segment_ids == segment_ids
    assert voice_repository.get_draft(1) is None
    assert repository.get_input(result.item_input.id, 1).voice_segment_ids == segment_ids
    with database.connection() as connection:
        input_row = connection.execute(
            "SELECT * FROM item_inputs WHERE id = ?",
            (result.item_input.id,),
        ).fetchone()
        associations = connection.execute(
            """
            SELECT draft_id, item_input_id FROM voice_segments
            WHERE item_input_id = ? ORDER BY position
            """,
            (result.item_input.id,),
        ).fetchall()
    assert input_row["source_draft_id"] == draft.id
    assert input_row["original_text"] == "  用户最终确认文本\n"
    assert [tuple(row) for row in associations] == [
        (None, result.item_input.id),
        (None, result.item_input.id),
    ]

    recovered = voice_repository.save_draft(1, draft.id, draft.revision)
    assert recovered.created is False
    assert recovered.item_input.id == result.item_input.id
    assert recovered.item_input.voice_segment_ids == segment_ids
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs"
        ).fetchone()[0] == 1


def test_text_only_final_save_uses_text_method_and_exact_text(tmp_path: Path):
    _, voice_repository, _ = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "\n text only \t", 0)

    result = voice_repository.save_draft(1, draft.id, draft.revision)

    assert result.item_input.input_method.value == "text"
    assert result.item_input.original_text == "\n text only \t"
    assert result.item_input.voice_segment_ids == []


@pytest.mark.parametrize("segment_status", ["pending", "transcribing", "failed"])
def test_final_save_blocks_every_non_succeeded_retained_segment(
    tmp_path: Path,
    segment_status: str,
):
    database, voice_repository, _ = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "not ready", 0)
    insert_segment(
        database,
        draft_id=draft.id,
        status=segment_status,
        suffix=segment_status,
    )

    with pytest.raises(DraftBlockedError):
        voice_repository.save_draft(1, draft.id, draft.revision)

    assert voice_repository.get_draft(1).current_text == "not ready"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs"
        ).fetchone()[0] == 0


def test_final_save_rejects_blank_stale_missing_and_cross_user_drafts(
    tmp_path: Path,
):
    _, voice_repository, _ = make_repository(tmp_path)
    blank = voice_repository.put_draft(1, "  \n\t", 0)

    with pytest.raises(DraftBlockedError, match="must not be blank"):
        voice_repository.save_draft(1, blank.id, blank.revision)
    with pytest.raises(DraftRevisionConflictError):
        voice_repository.save_draft(1, blank.id, blank.revision + 1)
    with pytest.raises(NotFoundError):
        voice_repository.save_draft(2, blank.id, blank.revision)
    with pytest.raises(NotFoundError):
        voice_repository.save_draft(1, blank.id + 100, blank.revision)


def test_final_save_failure_rolls_back_input_reparent_and_draft_delete(
    tmp_path: Path,
):
    database, voice_repository, _ = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "atomic", 0)
    segment_id = insert_segment(
        database,
        draft_id=draft.id,
        status="succeeded",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_final_save_draft_delete
            BEFORE DELETE ON capture_drafts
            BEGIN
                SELECT RAISE(ABORT, 'synthetic final save rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic final save rollback"):
        voice_repository.save_draft(1, draft.id, draft.revision)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs"
        ).fetchone()[0] == 0
        persisted_draft = connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()
        segment = connection.execute(
            "SELECT draft_id, item_input_id FROM voice_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    assert tuple(persisted_draft) == ("atomic", draft.revision)
    assert tuple(segment) == (draft.id, None)


def test_draft_discard_failure_rolls_back_ledger_and_metadata(tmp_path: Path):
    database, voice_repository, _ = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "discard atomically", 0)
    segment_id = insert_segment(
        database,
        draft_id=draft.id,
        status="failed",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_draft_discard
            BEFORE DELETE ON capture_drafts
            BEGIN
                SELECT RAISE(ABORT, 'synthetic discard rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic discard rollback"):
        voice_repository.delete_draft(1, draft.revision)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM capture_drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_failed_unlinked_input_delete_ledgers_voice_before_cascade(
    tmp_path: Path,
):
    database, voice_repository, repository = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "failed input", 0)
    segment_id = insert_segment(
        database,
        draft_id=draft.id,
        status="succeeded",
    )
    saved = voice_repository.save_draft(1, draft.id, draft.revision)
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE item_inputs
            SET processing_status = 'failed', failure_type = 'internal',
                failure_message = 'safe failure'
            WHERE id = ? AND user_id = 1
            """,
            (saved.item_input.id,),
        )

    repository.delete_failed_unlinked_input(saved.item_input.id, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()[0] == 0
        ledger = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchone()
    assert tuple(ledger) == ("original/aa/one.bin", "failed_input_delete")


def test_failed_input_delete_is_owner_state_scoped_and_transactional(
    tmp_path: Path,
):
    database, voice_repository, repository = make_repository(tmp_path)
    draft = voice_repository.put_draft(1, "delete rollback", 0)
    insert_segment(database, draft_id=draft.id, status="succeeded")
    saved = voice_repository.save_draft(1, draft.id, draft.revision)

    with pytest.raises(NotFoundError):
        repository.delete_failed_unlinked_input(saved.item_input.id, 2)
    with pytest.raises(InvalidOperationError):
        repository.delete_failed_unlinked_input(saved.item_input.id, 1)

    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE item_inputs
            SET processing_status = 'failed', failure_type = 'internal',
                failure_message = 'safe failure'
            WHERE id = ?
            """,
            (saved.item_input.id,),
        )
        connection.execute(
            """
            CREATE TRIGGER fail_input_delete
            BEFORE DELETE ON item_inputs
            BEGIN
                SELECT RAISE(ABORT, 'synthetic delete rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic delete rollback"):
        repository.delete_failed_unlinked_input(saved.item_input.id, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = ?",
            (saved.item_input.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE item_input_id = ?",
            (saved.item_input.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0
