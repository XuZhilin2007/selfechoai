from __future__ import annotations

import sqlite3

import pytest

from app.database import CURRENT_SCHEMA_VERSION, Database, DatabaseVersionError, SCHEMA_V8
from app.migrations import v009_voice_segment_state as migration
from app.voice_repository import (
    DraftBlockedError,
    VoiceCaptureRepository,
    VoiceSegmentConflictError,
)


NOW = "2026-09-23T00:00:00+00:00"
BATCH = "qwen-audio-3.0-asr-flash"
STREAMING = "qwen-audio-3.0-asr-flash-streaming"


def _seed_user_and_draft(connection: sqlite3.Connection, user_id: int) -> None:
    connection.execute(
        """INSERT INTO users
        (id, email, password_hash, display_name, timezone, status,
         password_changed_time, created_time, updated_time)
        VALUES (?, ?, 'hash', 'Test', 'UTC', 'active', ?, ?, ?)""",
        (user_id, f"user-{user_id}@example.com", NOW, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO capture_drafts
        (id, user_id, current_text, revision, created_time, updated_time)
        VALUES (?, ?, 'original draft', 1, ?, ?)""",
        (user_id, user_id, NOW, NOW),
    )


def _insert_segment(connection: sqlite3.Connection, user_id: int, status: str, model: str) -> None:
    started = NOW if status == "transcribing" else None
    finished = NOW if status in ("succeeded", "failed", "transcribed") else None
    transcript = "batch transcript" if status == "succeeded" else (
        "streaming transcript" if status == "transcribed" else None
    )
    failure = "network" if status == "failed" else None
    connection.execute(
        """INSERT INTO voice_segments
        (id, user_id, draft_id, position, client_segment_id, storage_key,
         original_size_bytes, original_sha256, transcription_status, provider,
         model, provider_transcript, failure_code, failure_message, attempt_count,
         created_time, updated_time, transcription_started_time, transcription_finished_time)
        VALUES (?, ?, ?, 0, ?, ?, 5, ?, ?, 'alibaba', ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
        (user_id, user_id, user_id, f"client-{user_id}",
         f"original/aa/{user_id}.webm", f"{user_id}" * 64, status, model,
         transcript, failure, "failed" if failure else None,
         NOW, NOW, started, finished),
    )


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    return {name: [tuple(row) for row in connection.execute(
        f'SELECT * FROM "{name}" ORDER BY rowid'
    )] for name in names}


def test_v8_migration_preserves_existing_voice_and_other_data(tmp_path):
    path = tmp_path / "v8.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_V8)
        for user_id, status in enumerate(("pending", "transcribing", "succeeded", "failed"), 1):
            _seed_user_and_draft(connection, user_id)
            _insert_segment(connection, user_id, status, BATCH)
        before = _snapshot(connection)
    with pytest.raises(DatabaseVersionError, match="explicit migration to version 9"):
        Database(path).initialize()
    assert migration.inspect_v8_database(path).voice_segment_count == 4
    assert migration.migrate_v8_to_v9(path).voice_segment_count == 4
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert _snapshot(connection) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert CURRENT_SCHEMA_VERSION == 9


def test_transcribed_is_durable_without_draft_acceptance_or_batch_retry(tmp_path):
    path = tmp_path / "v9.db"
    database = Database(path)
    database.initialize()
    with database.transaction() as connection:
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "transcribing", STREAMING)
        _seed_user_and_draft(connection, 2)
        _insert_segment(connection, 2, "pending", STREAMING)
    repository = VoiceCaptureRepository(database)
    assert repository.mark_segment_transcribed(
        1, 1, transcript="whole task final", provider_request_id="task-1"
    ).transcription_status == "transcribed"
    with database.connection() as connection:
        draft = connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id=1"
        ).fetchone()
        assert tuple(draft) == ("original draft", 1)
        segment = connection.execute(
            "SELECT provider_transcript, model, transcription_finished_time "
            "FROM voice_segments WHERE id=1"
        ).fetchone()
        assert segment["provider_transcript"] == "whole task final"
        assert segment["model"] == STREAMING
        assert segment["transcription_finished_time"] is not None
        assert connection.execute("SELECT COUNT(*) FROM item_inputs").fetchone()[0] == 0
    Database(path).initialize()
    reopened = VoiceCaptureRepository(Database(path))
    assert reopened.get_segment(1, 1).transcription_status == "transcribed"
    assert reopened.system_recover_interrupted_segments() == 0
    assert reopened.system_list_pending_segments() == []
    assert reopened.claim_pending_segment(2, 2) is False
    with pytest.raises(DraftBlockedError, match="only failed"):
        reopened.retry_failed_segment(1, 1)
    with pytest.raises(DraftBlockedError, match="transcription must finish"):
        reopened.save_draft(1, 1, 1)
    assert reopened.complete_transcription(
        1, 1, transcript="wrong", provider_request_id="other"
    ) is None
    assert repository.mark_segment_transcribed(
        1, 1, transcript="duplicate", provider_request_id="other"
    ) is None
    # The rejected duplicate does not retire recovery eligibility.
    assert [item.segment_id for item in reopened.system_list_transcribed_segments()] == [1]
    with database.connection() as connection:
        assert tuple(connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id=1"
        ).fetchone()) == ("original draft", 1)


def test_transcribed_acceptance_is_durable_idempotent_and_appends_once(tmp_path):
    path = tmp_path / "accept.db"
    database = Database(path)
    database.initialize()
    with database.transaction() as connection:
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "transcribing", STREAMING)
    repository = VoiceCaptureRepository(database)
    repository.mark_segment_transcribed(
        1, 1, transcript="final words", provider_request_id="task-1"
    )

    accepted = repository.accept_transcribed_segment(1, 1)
    assert accepted.transcription_status == "succeeded"
    with database.connection() as connection:
        draft = connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id=1"
        ).fetchone()
        assert tuple(draft) == ("original draft\nfinal words", 2)

    again = repository.accept_transcribed_segment(1, 1)
    assert again.transcription_status == "succeeded"
    with database.connection() as connection:
        draft = connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id=1"
        ).fetchone()
        assert tuple(draft) == ("original draft\nfinal words", 2)

    reopened = VoiceCaptureRepository(Database(path))
    assert reopened.accept_transcribed_segment(1, 1).transcription_status == "succeeded"
    with database.connection() as connection:
        draft = connection.execute(
            "SELECT current_text, revision FROM capture_drafts WHERE id=1"
        ).fetchone()
        assert tuple(draft) == ("original draft\nfinal words", 2)


def test_new_state_requires_complete_transcript_and_known_streaming_model(tmp_path):
    path = tmp_path / "v9-contracts.db"
    database = Database(path)
    database.initialize()
    with database.transaction() as connection:
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "transcribing", STREAMING)
    repository = VoiceCaptureRepository(database)
    with pytest.raises(VoiceSegmentConflictError, match="transcript"):
        repository.mark_segment_transcribed(1, 1, transcript="  ", provider_request_id="task")
    with pytest.raises(VoiceSegmentConflictError, match="request id"):
        repository.mark_segment_transcribed(1, 1, transcript="final", provider_request_id=" ")
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE voice_segments SET model='unknown-model' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE voice_segments SET transcription_status='transcribed', "
                "provider_transcript=' ', transcription_finished_time=? WHERE id=1",
                (NOW,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE voice_segments SET model=?, transcription_status='transcribed', "
                "provider_transcript='final', transcription_finished_time=? WHERE id=1",
                (BATCH, NOW),
            )
        assert connection.execute(
            "SELECT transcription_status FROM voice_segments WHERE id=1"
        ).fetchone()[0] == "transcribing"


def test_migration_failure_rolls_back_and_wrong_version_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "v8-failure.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_V8)
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "succeeded", BATCH)
        before = _snapshot(connection)
    original = migration._apply_schema_changes

    def fail_after_rebuild(connection):
        original(connection)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(migration, "_apply_schema_changes", fail_after_rebuild)
    with pytest.raises(migration.MigrationError, match="rolled back"):
        migration.migrate_v8_to_v9(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert _snapshot(connection) == before
    assert migration.inspect_v8_database(path).voice_segment_count == 1
    monkeypatch.setattr(migration, "_apply_schema_changes", original)
    migration.migrate_v8_to_v9(path)
    with pytest.raises(migration.MigrationError, match="expected database schema version 8"):
        migration.migrate_v8_to_v9(path)


def test_migration_keeps_autoincrement_high_water_mark(tmp_path):
    path = tmp_path / "v8-sequence.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_V8)
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "succeeded", BATCH)
        connection.execute("DELETE FROM voice_segments WHERE id=1")
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='voice_segments'"
        ).fetchone()[0] == 1
    assert migration.migrate_v8_to_v9(path).voice_segment_count == 0
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO voice_segments
            (user_id, draft_id, position, client_segment_id, storage_key,
             original_size_bytes, original_sha256, transcription_status,
             provider, model, provider_transcript, created_time, updated_time,
             transcription_finished_time)
            VALUES (1, 1, 0, 'next', 'original/aa/next.webm', 5, ?,
                    'succeeded', 'alibaba', ?, 'accepted text', ?, ?, ?)""",
            ("a" * 64, BATCH, NOW, NOW, NOW),
        )
        assert connection.execute("SELECT id FROM voice_segments").fetchone()[0] == 2


def test_cli_check_only_and_confirmation_are_read_only_or_explicit(tmp_path, capsys):
    path = tmp_path / "v8-cli.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_V8)
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "succeeded", BATCH)
    before = path.read_bytes()
    assert migration.main(["--database", str(path), "--check-only"]) == 0
    assert "Preflight passed" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert migration.main(
        ["--database", str(path)], input_fn=lambda _: "no",
    ) == 2
    assert path.read_bytes() == before
    assert migration.main(
        ["--database", str(path)], input_fn=lambda _: "MIGRATE PUBLIC V8 TO V9",
    ) == 0
    output = capsys.readouterr().out
    assert "Voice Segments preserved: 1" in output
    assert "Schema version is now 9." in output
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_startup_accepts_recovered_transcribed_segments(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    path = tmp_path / "recovery.db"
    database = Database(path)
    database.initialize()
    with database.transaction() as connection:
        _seed_user_and_draft(connection, 1)
        _insert_segment(connection, 1, "transcribing", STREAMING)
    repository = VoiceCaptureRepository(database)
    repository.mark_segment_transcribed(
        1, 1, transcript="startup words", provider_request_id="task-1"
    )
    settings = Settings(
        database_path=path,
        registration_mode="closed",
        app_origin="http://testserver",
        session_cookie_secure=False,
    )
    with TestClient(create_app(settings)):
        pass
    reopened = VoiceCaptureRepository(Database(path))
    segment = reopened.get_segment(1, 1)
    assert segment.transcription_status == "succeeded"
    draft = reopened.get_draft(1)
    assert draft.current_text == "original draft\nstartup words"
    assert draft.revision == 2
