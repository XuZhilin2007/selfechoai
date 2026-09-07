from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import Database
from app.services.voice_deletions import (
    ORPHAN_CLEANUP_GRACE_SECONDS,
    VOICE_DELETION_REASONS,
    VoiceDeletionLedger,
)
from app.services.voice_storage import VoiceStorage
from app.voice_repository import VoiceCaptureRepository


NOW = "2026-09-07T01:00:00+00:00"
AGED = "2000-01-01T00:00:00+00:00"


def make_foundation(
    tmp_path: Path,
) -> tuple[Database, VoiceStorage, VoiceDeletionLedger]:
    database = Database(tmp_path / "test.db")
    database.initialize()
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=1024)
    return database, storage, VoiceDeletionLedger(database, storage)


def add_user(database: Database, user_id: int = 1) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, email, password_hash, display_name, timezone, status,
                password_changed_time, created_time, updated_time
            ) VALUES (?, ?, 'hash', 'Voice', 'UTC', 'active', ?, ?, ?)
            """,
            (user_id, f"voice-{user_id}@example.com", NOW, NOW, NOW),
        )


def test_ledger_is_committed_before_physical_delete(tmp_path: Path):
    database, storage, ledger = make_foundation(tmp_path)
    saved = storage.store_original([b"synthetic audio"])

    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            [saved.storage_key],
            "draft_discard",
            created_time=NOW,
        )
        assert saved.path.is_file()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 1

    result = ledger.drain(limit=10)

    assert result.selected == 1
    assert result.deleted_or_absent == 1
    assert result.failed == 0
    assert not saved.path.exists()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_ledger_registration_rolls_back_with_caller_transaction(tmp_path: Path):
    database, storage, ledger = make_foundation(tmp_path)
    saved = storage.store_original([b"synthetic audio"])

    with pytest.raises(RuntimeError, match="rollback"):
        with database.transaction() as connection:
            VoiceDeletionLedger.record(
                connection,
                [saved.storage_key],
                "segment_delete",
            )
            raise RuntimeError("rollback")

    assert saved.path.is_file()
    assert ledger.drain().selected == 0


def test_allowed_reasons_and_repeat_registration_are_constrained_and_idempotent(
    tmp_path: Path,
):
    database, _, _ = make_foundation(tmp_path)
    with database.transaction() as connection:
        for index, reason in enumerate(sorted(VOICE_DELETION_REASONS)):
            key = f"original/aa/{index:032x}.bin"
            VoiceDeletionLedger.record(connection, [key, key], reason)
        with pytest.raises(ValueError, match="unsupported"):
            VoiceDeletionLedger.record(
                connection,
                ["original/aa/unknown.bin"],
                "unknown",
            )
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchall()
    assert len(rows) == len(VOICE_DELETION_REASONS)


def test_delete_failure_retains_file_and_updates_retry_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database, storage, ledger = make_foundation(tmp_path)
    saved = storage.store_original([b"synthetic audio"])
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            [saved.storage_key],
            "failed_input_delete",
        )

    def fail_delete(storage_key: str) -> bool:
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(storage, "delete_original", fail_delete)
    first = ledger.drain(limit=1)
    second = ledger.drain(limit=1)

    assert first.failed == second.failed == 1
    assert saved.path.is_file()
    with database.connection() as connection:
        row = connection.execute("SELECT * FROM voice_file_deletions").fetchone()
    assert row["attempt_count"] == 2
    assert row["last_attempt_time"] is not None
    assert "PermissionError" in row["last_error"]
    assert len(row["last_error"]) <= 500


def test_missing_file_is_retired_safely(tmp_path: Path):
    database, _, ledger = make_foundation(tmp_path)
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            ["original/aa/missing.bin"],
            "orphan_cleanup",
            created_time=AGED,
        )

    result = ledger.drain()

    assert result.deleted_or_absent == 1
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_orphan_grace_delays_recent_and_allows_aged_cleanup(tmp_path: Path):
    database, storage, ledger = make_foundation(tmp_path)
    recent = storage.store_original([b"active upload"])
    aged = storage.store_original([b"abandoned upload"])
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            [recent.storage_key],
            "orphan_cleanup",
        )
        VoiceDeletionLedger.record(
            connection,
            [aged.storage_key],
            "orphan_cleanup",
            created_time=AGED,
        )

    result = ledger.drain()

    assert result.selected == 1
    assert recent.path.is_file()
    assert not aged.path.exists()


def test_referenced_storage_key_is_never_deleted_as_orphan(tmp_path: Path):
    database, storage, ledger = make_foundation(tmp_path)
    add_user(database)
    repository = VoiceCaptureRepository(database)
    draft = repository.put_draft(1, "before", 0)
    saved = storage.store_original([b"referenced audio"])
    repository.create_pending_segment(
        user_id=1,
        client_segment_id="referenced-1",
        saved=saved,
        client_content_type="audio/webm",
        expected_revision=draft.revision,
    )
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            [saved.storage_key],
            "orphan_cleanup",
            created_time=AGED,
        )

    result = ledger.drain()

    assert result.selected == 0
    assert saved.path.is_file()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 1


def test_deletion_preserves_storage_path_containment(tmp_path: Path):
    database, storage, ledger = make_foundation(tmp_path)
    outside = storage.root / "outside.bin"
    outside.write_bytes(b"must survive")
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            ["original/../outside.bin"],
            "orphan_cleanup",
            created_time=AGED,
        )

    result = ledger.drain()

    assert result.failed == 1
    assert outside.read_bytes() == b"must survive"
    with database.connection() as connection:
        row = connection.execute("SELECT * FROM voice_file_deletions").fetchone()
    assert row["attempt_count"] == 1
    assert "InvalidVoiceStorageKeyError" in row["last_error"]


def test_drain_reclaims_only_stale_server_owned_parts(tmp_path: Path):
    _, storage, ledger = make_foundation(tmp_path)
    directory = storage.original_root / "aa"
    directory.mkdir(parents=True)
    stale = directory / f".{('a' * 32)}.part"
    recent = directory / f".{('b' * 32)}.part"
    unknown = directory / "manual.part"
    for path in (stale, recent, unknown):
        path.write_bytes(b"partial")
    old_timestamp = (
        datetime.now(timezone.utc)
        - timedelta(seconds=ORPHAN_CLEANUP_GRACE_SECONDS + 60)
    ).timestamp()
    os.utime(stale, (old_timestamp, old_timestamp))
    os.utime(unknown, (old_timestamp, old_timestamp))

    ledger.drain(limit=10)

    assert not stale.exists()
    assert recent.is_file()
    assert unknown.is_file()
