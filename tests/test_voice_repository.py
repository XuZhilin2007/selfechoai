from __future__ import annotations

from pathlib import Path

import pytest

from app.database import Database
from app.repository import NotFoundError
from app.services.voice_storage import VoiceStorage
from app.voice_repository import (
    DraftBlockedError,
    DraftRevisionConflictError,
    DraftTextLimitError,
    VoiceCaptureRepository,
    VoiceSegmentConflictError,
)


NOW = "2026-09-07T01:00:00+00:00"


def make_repository(
    tmp_path: Path,
) -> tuple[Database, VoiceCaptureRepository, VoiceStorage]:
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
    storage = VoiceStorage(tmp_path / "voice-data", max_upload_bytes=1024)
    return database, VoiceCaptureRepository(database), storage


@pytest.mark.parametrize(
    "text",
    ["", "   \n\t", "  leading", "trailing  ", "first\nsecond\n"],
)
def test_draft_persistence_preserves_exact_text(tmp_path: Path, text: str):
    _, repository, _ = make_repository(tmp_path)

    created = repository.put_draft(1, text, 0)

    assert created.current_text == text
    assert created.revision == 1
    assert repository.get_draft(1) == created


def test_draft_text_limit_accepts_exact_boundary_and_rejects_overflow(
    tmp_path: Path,
):
    _, repository, _ = make_repository(tmp_path)

    accepted = repository.put_draft(1, "x" * 10_000, 0)
    with pytest.raises(DraftTextLimitError, match="10,000"):
        repository.put_draft(1, "x" * 10_001, accepted.revision)

    assert repository.get_draft(1).current_text == "x" * 10_000


def test_revision_cas_prevents_stale_writer_overwrite(tmp_path: Path):
    _, repository, _ = make_repository(tmp_path)
    writer_a = repository.put_draft(1, "revision N", 0)
    writer_b = repository.put_draft(1, "writer B truth", writer_a.revision)

    with pytest.raises(DraftRevisionConflictError, match="stale"):
        repository.put_draft(1, "writer A stale text", writer_a.revision)

    current = repository.get_draft(1)
    assert current.current_text == "writer B truth"
    assert current.revision == writer_b.revision == 2


def test_each_user_has_one_independent_draft(tmp_path: Path):
    _, repository, _ = make_repository(tmp_path)

    first = repository.put_draft(1, "A", 0)
    second = repository.put_draft(2, "B", 0)

    assert first.id != second.id
    assert repository.get_draft(1).current_text == "A"
    assert repository.get_draft(2).current_text == "B"
    with pytest.raises(DraftRevisionConflictError):
        repository.put_draft(1, "duplicate create", 0)
    with pytest.raises(NotFoundError):
        repository.put_draft(999, "missing user", 0)


def test_draft_discard_is_revision_protected_and_records_segment_files(
    tmp_path: Path,
):
    database, repository, storage = make_repository(tmp_path)
    draft = repository.put_draft(1, "voice draft", 0)
    saved = storage.store_original([b"synthetic audio"])
    segment = repository.create_pending_segment(
        user_id=1,
        client_segment_id="segment-1",
        saved=saved,
        client_content_type="audio/webm",
        expected_revision=draft.revision,
    ).segment

    with pytest.raises(DraftRevisionConflictError):
        repository.delete_draft(1, draft.revision + 1)
    assert repository.get_segment(segment.id, 1).storage_key == saved.storage_key

    assert repository.delete_draft(1, draft.revision) is True
    assert repository.get_draft(1) is None
    assert repository.delete_draft(1, draft.revision) is False
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments"
        ).fetchone()[0] == 0
        deletion = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchone()
    assert tuple(deletion) == (saved.storage_key, "draft_discard")
    assert saved.path.is_file()


def test_segment_registration_is_ordered_idempotent_and_user_scoped(
    tmp_path: Path,
):
    database, repository, storage = make_repository(tmp_path)
    draft = repository.put_draft(1, "draft", 0)
    first_file = storage.store_original([b"first"])
    first = repository.create_pending_segment(
        user_id=1,
        client_segment_id="client.first",
        saved=first_file,
        client_content_type="audio/webm",
        expected_revision=draft.revision,
    )
    duplicate = repository.create_pending_segment(
        user_id=1,
        client_segment_id="client.first",
        saved=first_file,
        client_content_type="ignored-on-idempotent-retry",
        expected_revision=draft.revision,
    )

    assert first.created is True
    assert first.segment.position == 0
    assert duplicate.created is False
    assert duplicate.segment.id == first.segment.id

    conflicting_file = storage.store_original([b"different"])
    with pytest.raises(VoiceSegmentConflictError, match="different audio"):
        repository.create_pending_segment(
            user_id=1,
            client_segment_id="client.first",
            saved=conflicting_file,
            client_content_type="audio/webm",
            expected_revision=draft.revision,
        )
    with pytest.raises(DraftBlockedError, match="still being transcribed"):
        repository.create_pending_segment(
            user_id=1,
            client_segment_id="client.second",
            saved=conflicting_file,
            client_content_type="audio/webm",
            expected_revision=draft.revision,
        )

    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE voice_segments
            SET transcription_status = 'succeeded',
                provider_transcript = 'first transcript',
                provider_request_id = 'request-1',
                transcription_finished_time = ?, updated_time = ?
            WHERE id = ? AND user_id = ?
            """,
            (NOW, NOW, first.segment.id, 1),
        )
    second = repository.create_pending_segment(
        user_id=1,
        client_segment_id="client.second",
        saved=conflicting_file,
        client_content_type="audio/webm",
        expected_revision=draft.revision,
    )
    assert second.segment.position == 1
    assert [segment.id for segment in repository.get_draft(1).voice_segments] == [
        first.segment.id,
        second.segment.id,
    ]


def test_pending_claim_and_failed_delete_are_ownership_safe(tmp_path: Path):
    database, repository, storage = make_repository(tmp_path)
    draft_a = repository.put_draft(1, "A", 0)
    repository.put_draft(2, "B", 0)
    saved = storage.store_original([b"user-a-audio"])
    segment = repository.create_pending_segment(
        user_id=1,
        client_segment_id="user-a-segment",
        saved=saved,
        client_content_type=None,
        expected_revision=draft_a.revision,
    ).segment

    with pytest.raises(NotFoundError):
        repository.get_segment(segment.id, 2)
    assert repository.claim_pending_segment(segment.id, 2) is False
    with pytest.raises(NotFoundError):
        repository.delete_failed_segment(segment.id, 2)
    assert repository.get_draft(2).current_text == "B"
    assert repository.get_draft(1).current_text == "A"

    assert repository.claim_pending_segment(segment.id, 1) is True
    claimed = repository.get_segment_record(segment.id, 1)
    assert claimed.transcription_status == "transcribing"
    assert claimed.attempt_count == 1
    assert claimed.transcription_started_time is not None
    with pytest.raises(DraftBlockedError):
        repository.put_draft(1, "blocked", draft_a.revision)

    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE voice_segments
            SET transcription_status = 'failed',
                failure_code = 'network', failure_message = 'safe failure',
                transcription_finished_time = ?, updated_time = ?
            WHERE id = ? AND user_id = ?
            """,
            (NOW, NOW, segment.id, 1),
        )
    repository.delete_failed_segment(segment.id, 1)
    with pytest.raises(NotFoundError):
        repository.get_segment(segment.id, 1)
    with database.connection() as connection:
        deletion = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchone()
    assert tuple(deletion) == (saved.storage_key, "segment_delete")


@pytest.mark.parametrize(
    "client_segment_id",
    ["", " starts-with-space", "slash/not-allowed", "unicode-录音", "x" * 129],
)
def test_client_segment_id_contract_rejects_invalid_values(
    tmp_path: Path,
    client_segment_id: str,
):
    _, repository, storage = make_repository(tmp_path)
    draft = repository.put_draft(1, "draft", 0)
    saved = storage.store_original([b"audio"])

    with pytest.raises(ValueError, match="client_segment_id"):
        repository.create_pending_segment(
            user_id=1,
            client_segment_id=client_segment_id,
            saved=saved,
            client_content_type=None,
            expected_revision=draft.revision,
        )


def test_missing_original_is_rejected_before_segment_registration(tmp_path: Path):
    _, repository, storage = make_repository(tmp_path)
    draft = repository.put_draft(1, "draft", 0)
    saved = storage.store_original([b"audio"])
    saved.path.unlink()

    with pytest.raises(VoiceSegmentConflictError, match="no longer available"):
        repository.create_pending_segment(
            user_id=1,
            client_segment_id="missing-original",
            saved=saved,
            client_content_type=None,
            expected_revision=draft.revision,
        )

    assert repository.get_draft(1).voice_segments == ()
