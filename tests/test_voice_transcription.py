from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.database import Database
from app.services.alibaba_asr import (
    AlibabaASRAuthenticationError,
    AlibabaASRInvalidResponseError,
    AlibabaASRNetworkError,
    AlibabaASRQuotaError,
    AlibabaASRResult,
    AlibabaASRTimeoutError,
    AlibabaASRUnavailableError,
)
from app.services.voice_media import (
    MediaConversionError,
    MediaMetadata,
    MediaProbeError,
    PreparedASRAudio,
    UnsupportedMediaError,
)
from app.services.voice_storage import VoiceStorage
from app.services.voice_transcription import VoiceTranscriptionService
from app.voice_repository import (
    DraftTextLimitError,
    VoiceCaptureRepository,
)


NOW = "2026-09-07T01:00:00+00:00"


class FakeMediaProcessor:
    def __init__(
        self,
        *,
        probe_error: Exception | None = None,
        prepare_error: Exception | None = None,
        derived_path: Path | None = None,
    ) -> None:
        self.probe_error = probe_error
        self.prepare_error = prepare_error
        self.derived_path = derived_path
        self.probe_calls = 0
        self.prepare_calls = 0

    def probe(self, path: Path) -> MediaMetadata:
        self.probe_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        return MediaMetadata("webm", "opus", 48_000, 1, 1_000, "audio/webm")

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        if self.derived_path is None:
            yield PreparedASRAudio(path, "original_direct", "webm", None)
            return
        self.derived_path.write_bytes(b"synthetic derived wav")
        try:
            yield PreparedASRAudio(
                self.derived_path,
                "derived_wav",
                "wav",
                16_000,
            )
        finally:
            self.derived_path.unlink(missing_ok=True)


class FakeProvider:
    def __init__(
        self,
        transcript: str = "机器转写",
        error: Exception | None = None,
        hook=None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.hook = hook
        self.calls = 0
        self.arguments: list[tuple[Path, str, int | None]] = []

    async def transcribe(self, audio_path, *, format, sample_rate_hz):
        self.calls += 1
        self.arguments.append((audio_path, format, sample_rate_hz))
        assert audio_path.is_file()
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error
        return AlibabaASRResult(
            transcript=self.transcript,
            request_id=f"request-{self.calls}",
        )


class ThreadRecordingMediaProcessor(FakeMediaProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.probe_thread: int | None = None
        self.prepare_thread: int | None = None
        self.cleanup_thread: int | None = None

    def probe(self, path: Path) -> MediaMetadata:
        self.probe_thread = threading.get_ident()
        return super().probe(path)

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        self.prepare_thread = threading.get_ident()
        try:
            yield PreparedASRAudio(path, "original_direct", "webm", None)
        finally:
            self.cleanup_thread = threading.get_ident()


class BlockingProbeMediaProcessor(FakeMediaProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def probe(self, path: Path) -> MediaMetadata:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().probe(path)


class DraftDeletingMediaProcessor(FakeMediaProcessor):
    def __init__(self, repository: VoiceCaptureRepository) -> None:
        super().__init__()
        self.repository = repository

    def probe(self, path: Path) -> MediaMetadata:
        draft = self.repository.get_draft(1)
        assert draft is not None
        assert self.repository.delete_draft(1, draft.revision) is True
        return super().probe(path)


class BlockingPreparationMediaProcessor(FakeMediaProcessor):
    def __init__(self, derived_path: Path) -> None:
        super().__init__(derived_path=derived_path)
        self.entered = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        assert self.derived_path is not None
        self.derived_path.write_bytes(b"synthetic derived wav")
        self.entered.set()
        assert self.release.wait(timeout=5)
        try:
            yield PreparedASRAudio(self.derived_path, "derived_wav", "wav", 16_000)
        finally:
            self.derived_path.unlink(missing_ok=True)


class BlockingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered: asyncio.Event | None = None

    async def transcribe(self, audio_path, *, format, sample_rate_hz):
        self.calls += 1
        self.entered = self.entered or asyncio.Event()
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def make_context(
    tmp_path: Path,
    *,
    current_text: str = "",
    media: FakeMediaProcessor | None = None,
    provider: FakeProvider | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = Database(tmp_path / "test.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, email, password_hash, display_name, timezone, status,
                password_changed_time, created_time, updated_time
            ) VALUES (1, 'voice@example.com', 'hash', 'Voice', 'UTC',
                      'active', ?, ?, ?)
            """,
            (NOW, NOW, NOW),
        )
    repository = VoiceCaptureRepository(database)
    draft = repository.put_draft(1, current_text, 0)
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=1024)
    saved = storage.store_original([b"synthetic audio"])
    segment = repository.create_pending_segment(
        user_id=1,
        client_segment_id="client-1",
        saved=saved,
        client_content_type="untrusted/browser-type",
        expected_revision=draft.revision,
    ).segment
    media = media or FakeMediaProcessor()
    provider = provider or FakeProvider()
    service = VoiceTranscriptionService(repository, storage, media, provider)
    return database, repository, storage, service, provider, segment


def test_direct_success_appends_transcript_and_persists_metadata(tmp_path: Path):
    database, repository, storage, service, provider, segment = make_context(
        tmp_path,
        current_text="已有文字",
    )

    asyncio.run(service.process_initial(segment.id, 1))

    draft = repository.get_draft(1)
    completed = repository.get_segment(segment.id, 1)
    assert draft is not None
    assert draft.current_text == "已有文字\n机器转写"
    assert draft.revision == 2
    assert completed.transcription_status == "succeeded"
    assert completed.provider_transcript == "机器转写"
    assert completed.provider_request_id == "request-1"
    assert completed.detected_container == "webm"
    assert completed.detected_codec == "opus"
    assert completed.asr_input_kind == "original_direct"
    assert completed.attempt_count == 1
    assert provider.calls == 1
    assert provider.arguments[0][1:] == ("webm", None)
    assert storage.resolve_original(completed.storage_key).is_file()
    with database.connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_converted_success_sends_derived_only_and_always_cleans_it(tmp_path: Path):
    derived = tmp_path / "voice" / "tmp" / "derived.wav"
    media = FakeMediaProcessor(derived_path=derived)
    provider = FakeProvider()
    _, repository, storage, service, _, segment = make_context(
        tmp_path,
        media=media,
        provider=provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    completed = repository.get_segment(segment.id, 1)
    assert completed.transcription_status == "succeeded"
    assert completed.asr_input_kind == "derived_wav"
    assert provider.arguments[0][0] == derived
    assert provider.arguments[0][1:] == ("wav", 16_000)
    assert not derived.exists()
    assert storage.resolve_original(completed.storage_key).is_file()


@pytest.mark.parametrize(
    ("current_text", "transcript", "expected"),
    [
        ("", "transcript", "transcript"),
        ("text ", "transcript", "text transcript"),
        ("text", " transcript", "text transcript"),
        ("text", "transcript", "text\ntranscript"),
    ],
)
def test_transcript_whitespace_joining_matches_frozen_rule(
    tmp_path: Path,
    current_text: str,
    transcript: str,
    expected: str,
):
    provider = FakeProvider(transcript=transcript)
    _, repository, _, service, _, segment = make_context(
        tmp_path,
        current_text=current_text,
        provider=provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    assert repository.get_draft(1).current_text == expected


def test_completion_appends_to_latest_draft_without_overwriting_concurrent_edit(
    tmp_path: Path,
):
    database, repository, _, _, _, segment = make_context(
        tmp_path,
        current_text="A",
    )

    def mutate_to_latest_b() -> None:
        with database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE capture_drafts
                SET current_text = 'B', revision = revision + 1, updated_time = ?
                WHERE user_id = 1 AND revision = 1
                """,
                (NOW,),
            )
            assert cursor.rowcount == 1

    provider = FakeProvider(hook=mutate_to_latest_b)
    service = VoiceTranscriptionService(
        repository,
        VoiceStorage(tmp_path / "voice", max_upload_bytes=1024),
        FakeMediaProcessor(),
        provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    latest = repository.get_draft(1)
    assert latest is not None
    assert latest.current_text == "B\n机器转写"
    assert latest.revision == 3


@pytest.mark.parametrize(
    ("error", "failure_code"),
    [
        (MediaProbeError("raw probe details"), "media_probe"),
        (UnsupportedMediaError("raw duration details"), "unsupported_media"),
        (MediaConversionError("raw conversion details"), "conversion"),
    ],
)
def test_media_failures_are_persisted_safely_without_provider_call(
    tmp_path: Path,
    error: Exception,
    failure_code: str,
):
    provider = FakeProvider()
    if failure_code == "conversion":
        media = FakeMediaProcessor(prepare_error=error)
    else:
        media = FakeMediaProcessor(probe_error=error)
    _, repository, storage, service, _, segment = make_context(
        tmp_path,
        current_text="unchanged",
        media=media,
        provider=provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    failed = repository.get_segment(segment.id, 1)
    assert failed.transcription_status == "failed"
    assert failed.failure_code == failure_code
    assert "raw" not in failed.failure_message
    assert repository.get_draft(1).current_text == "unchanged"
    assert storage.resolve_original(failed.storage_key).is_file()
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("error", "failure_code"),
    [
        (AlibabaASRAuthenticationError("raw auth"), "authentication"),
        (AlibabaASRQuotaError("raw quota"), "quota_rate_limit"),
        (AlibabaASRTimeoutError("raw timeout"), "timeout"),
        (AlibabaASRNetworkError("raw network"), "network"),
        (AlibabaASRUnavailableError("raw unavailable"), "provider_unavailable"),
        (AlibabaASRInvalidResponseError("raw body"), "invalid_response"),
    ],
)
def test_provider_failures_are_durable_safe_and_never_hidden_retried(
    tmp_path: Path,
    error: Exception,
    failure_code: str,
):
    provider = FakeProvider(error=error)
    _, repository, storage, service, _, segment = make_context(
        tmp_path,
        current_text="unchanged",
        provider=provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    failed = repository.get_segment(segment.id, 1)
    assert failed.transcription_status == "failed"
    assert failed.failure_code == failure_code
    assert "raw" not in failed.failure_message
    assert failed.attempt_count == 1
    assert repository.get_draft(1).current_text == "unchanged"
    assert repository.system_list_pending_segments() == []
    assert storage.resolve_original(failed.storage_key).is_file()
    assert provider.calls == 1


def test_explicit_retry_is_required_and_attempt_count_changes_only_on_claim(
    tmp_path: Path,
):
    provider = FakeProvider(error=AlibabaASRNetworkError("first failure"))
    _, repository, _, service, _, segment = make_context(
        tmp_path,
        provider=provider,
    )
    asyncio.run(service.process_initial(segment.id, 1))
    assert repository.get_segment(segment.id, 1).attempt_count == 1
    assert provider.calls == 1

    provider.error = None
    pending, needs_processing = repository.retry_failed_segment(segment.id, 1)
    assert needs_processing is True
    assert pending.transcription_status == "pending"
    assert pending.attempt_count == 1
    assert provider.calls == 1

    asyncio.run(service.process_initial(segment.id, 1))
    completed = repository.get_segment(segment.id, 1)
    assert completed.transcription_status == "succeeded"
    assert completed.attempt_count == 2
    assert provider.calls == 2


def test_draft_text_limit_preserves_paid_transcript_and_reuses_without_provider(
    tmp_path: Path,
):
    provider = FakeProvider(transcript="十个字符的转写结果")
    _, repository, _, service, _, segment = make_context(
        tmp_path,
        current_text="x" * 9_995,
        provider=provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    blocked = repository.get_segment(segment.id, 1)
    draft = repository.get_draft(1)
    assert draft is not None
    assert blocked.transcription_status == "failed"
    assert blocked.failure_code == "draft_text_limit"
    assert blocked.provider_transcript == "十个字符的转写结果"
    assert blocked.provider_request_id == "request-1"
    assert blocked.attempt_count == 1
    assert draft.current_text == "x" * 9_995
    assert provider.calls == 1

    with pytest.raises(DraftTextLimitError):
        repository.retry_failed_segment(segment.id, 1)
    assert repository.get_segment(segment.id, 1).transcription_status == "failed"
    assert provider.calls == 1

    shortened = repository.put_draft(1, "缩短后的文字", draft.revision)
    completed, needs_processing = repository.retry_failed_segment(segment.id, 1)

    assert needs_processing is False
    assert completed.transcription_status == "succeeded"
    assert completed.attempt_count == 1
    assert repository.get_draft(1).current_text == "缩短后的文字\n十个字符的转写结果"
    assert repository.get_draft(1).revision == shortened.revision + 1
    assert provider.calls == 1


def test_draft_append_and_segment_success_are_one_transaction(tmp_path: Path):
    database, repository, _, service, provider, segment = make_context(
        tmp_path,
        current_text="before",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_draft_append
            AFTER UPDATE ON capture_drafts
            BEGIN
                SELECT RAISE(ABORT, 'synthetic append failure');
            END
            """
        )

    asyncio.run(service.process_initial(segment.id, 1))

    failed = repository.get_segment(segment.id, 1)
    assert provider.calls == 1
    assert repository.get_draft(1).current_text == "before"
    assert failed.transcription_status == "failed"
    assert failed.failure_code == "internal"
    assert failed.provider_transcript is None


def test_atomic_claim_prevents_duplicate_provider_calls(tmp_path: Path):
    _, repository, _, service, provider, segment = make_context(tmp_path)

    async def scenario() -> None:
        await asyncio.gather(
            service.process_initial(segment.id, 1),
            service.process_initial(segment.id, 1),
        )

    asyncio.run(scenario())

    assert provider.calls == 1
    assert repository.get_segment(segment.id, 1).attempt_count == 1


def test_draft_deleted_during_probe_stops_before_provider_transfer(tmp_path: Path):
    database, repository, storage, _, _, segment = make_context(tmp_path)
    provider = FakeProvider()
    service = VoiceTranscriptionService(
        repository,
        storage,
        DraftDeletingMediaProcessor(repository),
        provider,
    )

    asyncio.run(service.process_initial(segment.id, 1))

    assert provider.calls == 0
    assert repository.get_draft(1) is None
    with database.connection() as connection:
        deletion = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchone()
    assert tuple(deletion) == (segment.storage_key, "draft_discard")
    assert storage.resolve_original(segment.storage_key).is_file()


def test_media_work_runs_off_event_loop_and_other_coroutines_progress(
    tmp_path: Path,
):
    media = ThreadRecordingMediaProcessor()
    _, _, _, service, _, segment = make_context(tmp_path, media=media)
    event_loop_thread = threading.get_ident()

    asyncio.run(service.process_initial(segment.id, 1))

    assert media.probe_thread not in {None, event_loop_thread}
    assert media.prepare_thread not in {None, event_loop_thread}
    assert media.cleanup_thread not in {None, event_loop_thread}

    blocking = BlockingProbeMediaProcessor()
    _, _, _, blocking_service, _, blocking_segment = make_context(
        tmp_path / "blocking",
        media=blocking,
    )

    async def scenario() -> int:
        task = asyncio.create_task(
            blocking_service.process_initial(blocking_segment.id, 1)
        )
        ticks = 0
        while not blocking.entered.is_set():
            ticks += 1
            await asyncio.sleep(0)
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0)
        assert not task.done()
        blocking.release.set()
        await task
        return ticks

    assert asyncio.run(scenario()) >= 5


def test_cancellation_during_provider_wait_cleans_derived_and_marks_interrupted(
    tmp_path: Path,
):
    derived = tmp_path / "derived.wav"
    media = FakeMediaProcessor(derived_path=derived)
    provider = BlockingProvider()
    _, repository, storage, service, _, segment = make_context(
        tmp_path,
        media=media,
        provider=provider,
    )

    async def scenario() -> None:
        task = asyncio.create_task(service.process_initial(segment.id, 1))
        while provider.entered is None:
            await asyncio.sleep(0)
        await provider.entered.wait()
        assert derived.is_file()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    failed = repository.get_segment(segment.id, 1)
    assert failed.failure_code == "interrupted"
    assert not derived.exists()
    assert storage.resolve_original(failed.storage_key).is_file()


def test_cancellation_during_blocking_prepare_waits_for_cleanup(tmp_path: Path):
    derived = tmp_path / "preparing.wav"
    media = BlockingPreparationMediaProcessor(derived)
    provider = FakeProvider()
    _, repository, _, service, _, segment = make_context(
        tmp_path,
        media=media,
        provider=provider,
    )

    async def scenario() -> None:
        task = asyncio.create_task(service.process_initial(segment.id, 1))
        while not media.entered.is_set():
            await asyncio.sleep(0)
        assert derived.is_file()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        media.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert provider.calls == 0
    assert not derived.exists()
    assert repository.get_segment(segment.id, 1).failure_code == "interrupted"


def test_startup_recovery_fails_transcribing_and_keeps_pending_as_initial_work(
    tmp_path: Path,
):
    _, repository, _, _, _, segment = make_context(tmp_path)
    assert repository.claim_pending_segment(segment.id, 1) is True

    recovered = repository.system_recover_interrupted_segments()

    assert recovered == 1
    failed = repository.get_segment(segment.id, 1)
    assert failed.failure_code == "interrupted"
    assert failed.attempt_count == 1
    assert repository.system_list_pending_segments() == []
