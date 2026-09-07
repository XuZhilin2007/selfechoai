from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import MAX_VOICE_UPLOAD_BYTES, PROJECT_ROOT, Settings
from app.services.voice_deletions import VoiceDeletionDrainResult
from app.voice_contracts import DEFAULT_VOICE_MAX_UPLOAD_BYTES
from app.voice_repository import PendingVoiceSegment
from app.voice_runtime import (
    VoiceRuntime,
    VoiceRuntimeConfigurationError,
    validate_voice_runtime,
)


def enabled_settings(storage_root: Path, **overrides) -> Settings:
    executable = Path(sys.executable).resolve()
    values = {
        "database_path": storage_root.parent / "test.db",
        "voice_asr_enabled": True,
        "voice_storage_root": storage_root,
        "ffprobe_path": executable,
        "ffmpeg_path": executable,
        "alibaba_api_key": SecretStr("synthetic-test-key"),
    }
    values.update(overrides)
    return Settings(**values)


def test_disabled_voice_requires_nothing_and_touches_nothing(tmp_path: Path):
    missing_root = tmp_path / "must-not-exist"
    settings = Settings(
        database_path=tmp_path / "test.db",
        voice_asr_enabled=False,
        voice_storage_root=missing_root,
    )

    assert validate_voice_runtime(settings) is None
    assert not missing_root.exists()


def test_enabled_voice_validates_external_storage_and_executables(tmp_path: Path):
    storage_root = (tmp_path / "voice-root").resolve()

    paths = validate_voice_runtime(enabled_settings(storage_root))

    assert paths is not None
    assert paths.storage_root == storage_root
    assert paths.ffprobe_path == Path(sys.executable).resolve()
    assert paths.ffmpeg_path == Path(sys.executable).resolve()
    assert (storage_root / "original").is_dir()
    assert (storage_root / "tmp").is_dir()
    assert list(storage_root.glob(".selfecho-write-test-*")) == []


def test_enabled_voice_requires_all_settings_before_writing_storage(tmp_path: Path):
    storage_root = (tmp_path / "must-not-exist").resolve()
    settings = Settings(
        database_path=tmp_path / "test.db",
        voice_asr_enabled=True,
        voice_storage_root=storage_root,
    )

    with pytest.raises(VoiceRuntimeConfigurationError) as captured:
        validate_voice_runtime(settings)

    message = str(captured.value)
    assert "FFPROBE_PATH" in message
    assert "FFMPEG_PATH" in message
    assert "ALIBABA_API_KEY" in message
    assert "synthetic-test-key" not in message
    assert not storage_root.exists()


def test_enabled_voice_rejects_relative_and_checkout_storage(tmp_path: Path):
    with pytest.raises(VoiceRuntimeConfigurationError, match="absolute path"):
        validate_voice_runtime(enabled_settings(Path("relative/voice")))

    with pytest.raises(VoiceRuntimeConfigurationError, match="outside the Git checkout"):
        validate_voice_runtime(enabled_settings(PROJECT_ROOT / "data" / "voice"))


def test_enabled_voice_rejects_missing_or_relative_executables(tmp_path: Path):
    storage = (tmp_path / "voice").resolve()
    with pytest.raises(VoiceRuntimeConfigurationError, match="absolute path"):
        validate_voice_runtime(
            enabled_settings(storage, ffprobe_path=Path("ffprobe"))
        )
    with pytest.raises(VoiceRuntimeConfigurationError, match="executable file"):
        validate_voice_runtime(
            enabled_settings(storage, ffmpeg_path=(tmp_path / "missing").resolve())
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.invalid/asr",
        "https://user:password@example.invalid/asr",
        "https://example.invalid/asr#fragment",
        "https://[invalid",
        "not-a-url",
    ],
)
def test_enabled_voice_rejects_invalid_endpoint_before_storage_write(
    tmp_path: Path,
    endpoint: str,
):
    storage = (tmp_path / "must-not-exist").resolve()
    with pytest.raises(VoiceRuntimeConfigurationError, match="HTTPS endpoint"):
        validate_voice_runtime(
            enabled_settings(storage, alibaba_asr_api_url=endpoint)
        )
    assert not storage.exists()


def test_operator_custom_https_endpoint_is_allowed(tmp_path: Path):
    storage = (tmp_path / "voice").resolve()
    paths = validate_voice_runtime(
        enabled_settings(
            storage,
            alibaba_asr_api_url="https://operator.example.invalid/custom/asr?region=one",
        )
    )
    assert paths is not None


def test_voice_environment_configuration_is_parsed(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "voice.env"
    executable = Path(sys.executable).resolve()
    root = (tmp_path / "voice-root").resolve()
    env_file.write_text(
        "\n".join(
            [
                "VOICE_ASR_ENABLED=true",
                f"VOICE_STORAGE_ROOT={root}",
                "VOICE_MAX_UPLOAD_BYTES=1024",
                f"FFPROBE_PATH={executable}",
                f"FFMPEG_PATH={executable}",
                "ALIBABA_ASR_API_URL=https://example.invalid/v1",
                "ALIBABA_API_KEY=synthetic-test-key",
                "VOICE_ASR_TIMEOUT_SECONDS=12.5",
            ]
        ),
        encoding="utf-8",
    )
    names = (
        "VOICE_ASR_ENABLED",
        "VOICE_STORAGE_ROOT",
        "VOICE_MAX_UPLOAD_BYTES",
        "FFPROBE_PATH",
        "FFMPEG_PATH",
        "ALIBABA_ASR_API_URL",
        "ALIBABA_API_KEY",
        "VOICE_ASR_TIMEOUT_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.voice_asr_enabled is True
    assert settings.voice_storage_root == root
    assert settings.voice_max_upload_bytes == 1024
    assert settings.ffprobe_path == executable
    assert settings.ffmpeg_path == executable
    assert settings.alibaba_asr_api_url == "https://example.invalid/v1"
    assert settings.alibaba_api_key.get_secret_value() == "synthetic-test-key"
    assert settings.voice_asr_timeout_seconds == 12.5


def test_voice_disabled_defaults_are_safe(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    for name in (
        "VOICE_ASR_ENABLED",
        "VOICE_STORAGE_ROOT",
        "VOICE_MAX_UPLOAD_BYTES",
        "FFPROBE_PATH",
        "FFMPEG_PATH",
        "ALIBABA_ASR_API_URL",
        "ALIBABA_API_KEY",
        "VOICE_ASR_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.voice_asr_enabled is False
    assert settings.voice_storage_root is None
    assert settings.voice_max_upload_bytes == DEFAULT_VOICE_MAX_UPLOAD_BYTES
    assert settings.ffprobe_path is None
    assert settings.ffmpeg_path is None
    assert settings.alibaba_api_key.get_secret_value() == ""
    assert settings.voice_asr_timeout_seconds == 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("voice_max_upload_bytes", 0),
        ("voice_max_upload_bytes", MAX_VOICE_UPLOAD_BYTES + 1),
        ("voice_max_upload_bytes", True),
        ("voice_asr_timeout_seconds", 0),
        ("voice_asr_timeout_seconds", True),
        ("voice_asr_timeout_seconds", float("nan")),
        ("voice_asr_timeout_seconds", float("inf")),
        ("voice_asr_timeout_seconds", 121),
    ],
)
def test_voice_numeric_configuration_is_finite_positive_and_bounded(
    tmp_path: Path,
    field: str,
    value: object,
):
    with pytest.raises(ValueError, match="VOICE_"):
        Settings(database_path=tmp_path / "invalid.db", **{field: value})


class RuntimeRepository:
    def __init__(self, pending: list[PendingVoiceSegment]) -> None:
        self.pending = pending
        self.recovery_calls = 0

    def system_recover_interrupted_segments(self) -> int:
        self.recovery_calls += 1
        return 1

    def system_list_pending_segments(self) -> list[PendingVoiceSegment]:
        return list(self.pending)


class RuntimeLedger:
    def __init__(self) -> None:
        self.calls = 0
        self.thread_id: int | None = None

    def drain(self, *, limit: int) -> VoiceDeletionDrainResult:
        self.calls += 1
        self.thread_id = threading.get_ident()
        assert limit == 25
        return VoiceDeletionDrainResult(0, 0, 0)


class RuntimeProcessor:
    def __init__(self, *, fail: bool = False, block: bool = False) -> None:
        self.fail = fail
        self.block = block
        self.calls: list[tuple[int, int]] = []
        self.entered: asyncio.Event | None = None
        self.cancelled = False

    async def process_initial(self, segment_id: int, user_id: int) -> None:
        self.calls.append((segment_id, user_id))
        if self.fail:
            raise RuntimeError("synthetic task failure")
        if self.block:
            self.entered = self.entered or asyncio.Event()
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


def test_runtime_recovers_drains_off_loop_and_resumes_pending_once():
    repository = RuntimeRepository(
        [PendingVoiceSegment(1, 10), PendingVoiceSegment(2, 20)]
    )
    ledger = RuntimeLedger()
    processor = RuntimeProcessor()

    async def scenario() -> None:
        event_loop_thread = threading.get_ident()
        runtime = VoiceRuntime(repository, processor, ledger)
        await runtime.start()
        await runtime.start()
        for _ in range(3):
            await asyncio.sleep(0)
        assert repository.recovery_calls == 1
        assert ledger.calls == 1
        assert ledger.thread_id not in {None, event_loop_thread}
        assert processor.calls == [(1, 10), (2, 20)]
        assert runtime.active_task_count == 0
        await runtime.stop()

    asyncio.run(scenario())


def test_runtime_prevents_duplicate_schedule_and_cancels_on_shutdown():
    repository = RuntimeRepository([])
    ledger = RuntimeLedger()
    processor = RuntimeProcessor(block=True)

    async def scenario() -> None:
        runtime = VoiceRuntime(repository, processor, ledger)
        assert runtime.schedule(1, 1) is False
        await runtime.start()
        assert runtime.schedule(1, 1) is True
        assert runtime.schedule(1, 1) is False
        while processor.entered is None:
            await asyncio.sleep(0)
        await processor.entered.wait()
        await runtime.stop()
        assert processor.cancelled is True
        assert runtime.active_task_count == 0

    asyncio.run(scenario())


def test_runtime_consumes_unexpected_task_exception():
    repository = RuntimeRepository([PendingVoiceSegment(1, 1)])
    ledger = RuntimeLedger()
    processor = RuntimeProcessor(fail=True)

    async def scenario() -> list[dict]:
        contexts: list[dict] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda current_loop, context: contexts.append(context))
        runtime = VoiceRuntime(repository, processor, ledger)
        await runtime.start()
        for _ in range(3):
            await asyncio.sleep(0)
        assert runtime.active_task_count == 0
        await runtime.stop()
        return contexts

    assert asyncio.run(scenario()) == []
