from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.services.ai import DisabledAIService
from app.services.alibaba_asr import AlibabaASRResult
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_media import MediaMetadata, PreparedASRAudio
from app.services.voice_storage import VoiceStorage
from app.voice_repository import VoiceCaptureRepository
from app.voice_runtime import VoiceRuntimeConfigurationError


NOW = "2026-09-07T01:00:00+00:00"


class RecoveryMediaProcessor:
    def probe(self, path: Path) -> MediaMetadata:
        assert path.read_bytes().startswith(b"synthetic")
        return MediaMetadata("webm", "opus", 48_000, 1, 500, "audio/webm")

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        yield PreparedASRAudio(path, "original_direct", "webm", None)


class RecoveryProvider:
    def __init__(self, *, block: bool = False, fail_if_called: bool = False) -> None:
        self.block = block
        self.fail_if_called = fail_if_called
        self.calls = 0
        self.entered = threading.Event()

    async def transcribe(self, audio_path, *, format, sample_rate_hz):
        self.calls += 1
        self.entered.set()
        if self.fail_if_called:
            raise AssertionError("Provider must not be called during startup validation")
        if self.block:
            await asyncio.Event().wait()
        return AlibabaASRResult("启动恢复转写", f"recovery-{self.calls}")


def voice_settings(
    database_path: Path,
    *,
    enabled: bool,
    storage_root: Path | None = None,
    api_key: str | None = None,
) -> Settings:
    executable = Path(sys.executable).resolve()
    return Settings(
        database_path=database_path,
        ai_provider="disabled",
        voice_asr_enabled=enabled,
        voice_storage_root=storage_root,
        ffprobe_path=executable if enabled else None,
        ffmpeg_path=executable if enabled else None,
        alibaba_api_key=SecretStr(
            api_key if api_key is not None else ("synthetic-test-key" if enabled else "")
        ),
    )


def add_users(database: Database, count: int) -> None:
    with database.transaction() as connection:
        for user_id in range(1, count + 1):
            connection.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, display_name, timezone, status,
                    password_changed_time, created_time, updated_time
                ) VALUES (?, ?, 'hash', ?, 'UTC', 'active', ?, ?, ?)
                """,
                (
                    user_id,
                    f"runtime-{user_id}@example.com",
                    f"Runtime {user_id}",
                    NOW,
                    NOW,
                    NOW,
                ),
            )


def add_segment(
    repository: VoiceCaptureRepository,
    storage: VoiceStorage,
    *,
    user_id: int,
    client_id: str,
):
    draft = repository.put_draft(user_id, f"Draft {user_id}", 0)
    saved = storage.store_original([f"synthetic-{user_id}".encode()])
    segment = repository.create_pending_segment(
        user_id=user_id,
        client_segment_id=client_id,
        saved=saved,
        client_content_type="audio/webm",
        expected_revision=draft.revision,
    ).segment
    return segment, saved


def test_disabled_startup_is_inert_and_preserves_normal_public_startup(
    tmp_path: Path,
):
    untouched_root = (tmp_path / "must-not-exist").resolve()
    provider = RecoveryProvider(fail_if_called=True)
    app = create_app(
        settings=voice_settings(
            tmp_path / "disabled.db",
            enabled=False,
            storage_root=untouched_root,
        ),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.voice_repository is not None
        assert app.state.voice_storage is None
        assert app.state.voice_deletion_ledger is None
        assert app.state.voice_transcription_service is None
        assert app.state.voice_runtime is None

    assert provider.calls == 0
    assert not untouched_root.exists()


def test_disabled_startup_only_recovers_stale_transcribing_without_processing(
    tmp_path: Path,
):
    database_path = tmp_path / "disabled-recovery.db"
    storage_root = (tmp_path / "existing-voice-root").resolve()
    database = Database(database_path)
    database.initialize()
    add_users(database, 1)
    repository = VoiceCaptureRepository(database)
    storage = VoiceStorage(storage_root, max_upload_bytes=1024)
    segment, original = add_segment(
        repository,
        storage,
        user_id=1,
        client_id="disabled-interrupted",
    )
    assert repository.claim_pending_segment(segment.id, 1) is True
    provider = RecoveryProvider(fail_if_called=True)
    app = create_app(
        settings=voice_settings(database_path, enabled=False),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with TestClient(app):
        recovered = repository.get_segment(segment.id, 1)
        assert recovered.transcription_status == "failed"
        assert recovered.failure_code == "interrupted"

    assert provider.calls == 0
    assert original.path.is_file()


def test_invalid_enabled_startup_fails_without_provider_call_or_storage_write(
    tmp_path: Path,
):
    storage_root = (tmp_path / "must-not-exist").resolve()
    provider = RecoveryProvider(fail_if_called=True)
    app = create_app(
        settings=voice_settings(
            tmp_path / "invalid.db",
            enabled=True,
            storage_root=storage_root,
            api_key="",
        ),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with pytest.raises(VoiceRuntimeConfigurationError, match="ALIBABA_API_KEY"):
        with TestClient(app):
            pass

    assert provider.calls == 0
    assert not storage_root.exists()


def test_valid_enabled_startup_does_not_probe_provider_when_nothing_is_pending(
    tmp_path: Path,
):
    storage_root = (tmp_path / "voice-root").resolve()
    provider = RecoveryProvider(fail_if_called=True)
    app = create_app(
        settings=voice_settings(
            tmp_path / "empty.db",
            enabled=True,
            storage_root=storage_root,
        ),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert provider.calls == 0
    assert storage_root.is_dir()


def test_enabled_startup_recovers_resumes_and_drains_without_failed_retry(
    tmp_path: Path,
):
    database_path = tmp_path / "recovery.db"
    storage_root = (tmp_path / "voice-root").resolve()
    database = Database(database_path)
    database.initialize()
    add_users(database, 3)
    repository = VoiceCaptureRepository(database)
    storage = VoiceStorage(storage_root, max_upload_bytes=1024)

    interrupted, interrupted_file = add_segment(
        repository,
        storage,
        user_id=1,
        client_id="startup-transcribing",
    )
    assert repository.claim_pending_segment(interrupted.id, 1) is True

    pending, pending_file = add_segment(
        repository,
        storage,
        user_id=2,
        client_id="startup-pending",
    )

    failed, failed_file = add_segment(
        repository,
        storage,
        user_id=3,
        client_id="startup-failed",
    )
    assert repository.claim_pending_segment(failed.id, 3) is True
    assert repository.mark_segment_failed(
        failed.id,
        3,
        failure_code="network",
        failure_message="controlled failure",
    ) is True

    orphan = storage.store_original([b"synthetic-orphan"])
    with database.transaction() as connection:
        VoiceDeletionLedger.record(
            connection,
            [orphan.storage_key],
            "orphan_cleanup",
            created_time="2000-01-01T00:00:00+00:00",
        )

    provider = RecoveryProvider()
    app = create_app(
        settings=voice_settings(
            database_path,
            enabled=True,
            storage_root=storage_root,
        ),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with TestClient(app):
        for _ in range(100):
            if repository.get_segment(pending.id, 2).transcription_status == "succeeded":
                break
            time.sleep(0.01)

        recovered = repository.get_segment(interrupted.id, 1)
        resumed = repository.get_segment(pending.id, 2)
        still_failed = repository.get_segment(failed.id, 3)
        assert recovered.transcription_status == "failed"
        assert recovered.failure_code == "interrupted"
        assert recovered.attempt_count == 1
        assert resumed.transcription_status == "succeeded"
        assert resumed.attempt_count == 1
        assert still_failed.transcription_status == "failed"
        assert still_failed.failure_code == "network"
        assert still_failed.attempt_count == 1
        assert provider.calls == 1
        assert repository.get_draft(2).current_text == "Draft 2\n启动恢复转写"
        assert app.state.voice_runtime.startup_drain_result.selected == 1
        assert not orphan.path.exists()

    for original in (interrupted_file, pending_file, failed_file):
        assert original.path.is_file()


def test_shutdown_cancels_pending_runtime_task_and_preserves_original(
    tmp_path: Path,
):
    database_path = tmp_path / "shutdown.db"
    storage_root = (tmp_path / "voice-root").resolve()
    database = Database(database_path)
    database.initialize()
    add_users(database, 1)
    repository = VoiceCaptureRepository(database)
    storage = VoiceStorage(storage_root, max_upload_bytes=1024)
    segment, original = add_segment(
        repository,
        storage,
        user_id=1,
        client_id="shutdown-pending",
    )
    provider = RecoveryProvider(block=True)
    app = create_app(
        settings=voice_settings(
            database_path,
            enabled=True,
            storage_root=storage_root,
        ),
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=RecoveryMediaProcessor(),
    )

    with TestClient(app):
        assert provider.entered.wait(timeout=5)

    stopped = repository.get_segment(segment.id, 1)
    assert stopped.transcription_status == "failed"
    assert stopped.failure_code == "interrupted"
    assert original.path.is_file()
