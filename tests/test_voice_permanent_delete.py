from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import Database
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_storage import StoredOriginal, VoiceStorage


NOW = "2026-09-07T01:00:00+00:00"


def make_foundation(
    tmp_path: Path,
) -> tuple[Database, Repository, VoiceStorage, VoiceDeletionLedger]:
    database = Database(tmp_path / "test.db")
    database.initialize()
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=1024)
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
    return (
        database,
        Repository(database),
        storage,
        VoiceDeletionLedger(database, storage),
    )


def add_item_with_input(
    database: Database,
    *,
    item_id: int,
    input_id: int,
    user_id: int = 1,
    status: str = "trash",
    input_method: str = "voice",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO personal_items (
                id, user_id, title, type, importance, urgency, status,
                created_time, updated_time
            ) VALUES (?, ?, ?, 'note', 'unknown', 'unknown', ?, ?, ?)
            """,
            (item_id, user_id, f"Item {item_id}", status, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO item_inputs (
                id, user_id, item_id, original_text, input_method,
                processing_status, created_time
            ) VALUES (?, ?, ?, ?, ?, 'succeeded', ?)
            """,
            (
                input_id,
                user_id,
                item_id,
                f"Original input {input_id}",
                input_method,
                NOW,
            ),
        )


def add_succeeded_segment(
    database: Database,
    *,
    user_id: int,
    item_input_id: int,
    position: int,
    saved: StoredOriginal,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, item_input_id, position, client_segment_id,
                storage_key, original_size_bytes, original_sha256,
                client_content_type, transcription_status, provider, model,
                provider_transcript, created_time, updated_time,
                transcription_finished_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'audio/webm', 'succeeded',
                      'alibaba', 'qwen-audio-3.0-asr-flash', ?, ?, ?, ?)
            """,
            (
                user_id,
                item_input_id,
                position,
                f"input-{item_input_id}-segment-{position}",
                saved.storage_key,
                saved.size_bytes,
                saved.sha256,
                f"Transcript {position}",
                NOW,
                NOW,
                NOW,
            ),
        )


def test_owner_permanent_delete_records_only_associated_voice_files_then_drain(
    tmp_path: Path,
):
    database, repository, storage, ledger = make_foundation(tmp_path)
    add_item_with_input(database, item_id=11, input_id=101)
    add_item_with_input(database, item_id=12, input_id=102)
    first = storage.store_original([b"first target audio"])
    second = storage.store_original([b"second target audio"])
    unrelated = storage.store_original([b"unrelated audio"])
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=101,
        position=0,
        saved=first,
    )
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=101,
        position=1,
        saved=second,
    )
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=102,
        position=0,
        saved=unrelated,
    )

    repository.permanently_delete_item(11, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = 11 AND user_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = 101 AND user_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE item_input_id = 101"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE item_input_id = 102"
        ).fetchone()[0] == 1
        deletion_rows = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions ORDER BY storage_key"
        ).fetchall()
    assert [(row["storage_key"], row["reason"]) for row in deletion_rows] == [
        (key, "item_permanent_delete")
        for key in sorted((first.storage_key, second.storage_key))
    ]
    assert first.path.is_file()
    assert second.path.is_file()
    assert unrelated.path.is_file()

    result = ledger.drain(limit=10)

    assert result.selected == 2
    assert result.deleted_or_absent == 2
    assert result.failed == 0
    assert not first.path.exists()
    assert not second.path.exists()
    assert unrelated.path.is_file()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_non_owner_and_non_trash_item_cannot_register_or_delete(
    tmp_path: Path,
):
    database, repository, storage, _ = make_foundation(tmp_path)
    add_item_with_input(database, item_id=21, input_id=201)
    add_item_with_input(
        database,
        item_id=22,
        input_id=202,
        status="active",
    )
    trash_audio = storage.store_original([b"trash owner audio"])
    active_audio = storage.store_original([b"active owner audio"])
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=201,
        position=0,
        saved=trash_audio,
    )
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=202,
        position=0,
        saved=active_audio,
    )

    with pytest.raises(NotFoundError, match="item not found"):
        repository.permanently_delete_item(21, 2)
    with pytest.raises(InvalidOperationError, match="must be in trash"):
        repository.permanently_delete_item(22, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id IN (21, 22)"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE item_input_id IN (201, 202)"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0
    assert trash_audio.path.is_file()
    assert active_audio.path.is_file()


def test_permanent_delete_failure_rolls_back_ledger_and_item_cascade(
    tmp_path: Path,
):
    database, repository, storage, _ = make_foundation(tmp_path)
    add_item_with_input(database, item_id=31, input_id=301)
    saved = storage.store_original([b"rollback audio"])
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=301,
        position=0,
        saved=saved,
    )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_item_31_delete
            AFTER DELETE ON personal_items
            WHEN OLD.id = 31
            BEGIN
                SELECT RAISE(ABORT, 'synthetic permanent delete failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        repository.permanently_delete_item(31, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = 31 AND user_id = 1"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = 301 AND user_id = 1"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE item_input_id = 301"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0
    assert saved.path.is_file()


def test_item_deletion_drain_failure_keeps_ledger_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database, repository, storage, ledger = make_foundation(tmp_path)
    add_item_with_input(database, item_id=41, input_id=401)
    saved = storage.store_original([b"retry audio"])
    add_succeeded_segment(
        database,
        user_id=1,
        item_input_id=401,
        position=0,
        saved=saved,
    )
    repository.permanently_delete_item(41, 1)
    real_delete = storage.delete_original
    attempts = 0

    def fail_once(storage_key: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic locked file")
        return real_delete(storage_key)

    monkeypatch.setattr(storage, "delete_original", fail_once)

    first = ledger.drain(limit=1)

    assert first.selected == 1
    assert first.failed == 1
    assert saved.path.is_file()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT reason, attempt_count, last_error FROM voice_file_deletions"
        ).fetchone()
    assert row["reason"] == "item_permanent_delete"
    assert row["attempt_count"] == 1
    assert "PermissionError" in row["last_error"]

    second = ledger.drain(limit=1)

    assert second.selected == 1
    assert second.deleted_or_absent == 1
    assert second.failed == 0
    assert not saved.path.exists()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_non_voice_item_permanent_delete_behavior_is_unchanged(tmp_path: Path):
    database, repository, _, ledger = make_foundation(tmp_path)
    add_item_with_input(
        database,
        item_id=51,
        input_id=501,
        input_method="text",
    )

    repository.permanently_delete_item(51, 1)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = 51 AND user_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = 501 AND user_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0
    assert ledger.drain(limit=1).selected == 0
