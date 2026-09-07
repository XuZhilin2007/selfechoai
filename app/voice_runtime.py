from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from app.config import PROJECT_ROOT, Settings
from app.services.voice_deletions import VoiceDeletionDrainResult, VoiceDeletionLedger
from app.voice_repository import VoiceCaptureRepository


class VoiceRuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VoiceRuntimePaths:
    storage_root: Path
    ffprobe_path: Path
    ffmpeg_path: Path


class VoiceSegmentProcessor(Protocol):
    async def process_initial(self, segment_id: int, user_id: int) -> None: ...


class VoiceRuntime:
    """Single-instance task coordinator with no periodic or automatic retry loop."""

    def __init__(
        self,
        repository: VoiceCaptureRepository,
        processor: VoiceSegmentProcessor,
        deletion_ledger: VoiceDeletionLedger,
        *,
        deletion_drain_limit: int = 25,
    ) -> None:
        if deletion_drain_limit <= 0:
            raise ValueError("deletion_drain_limit must be positive")
        self.repository = repository
        self.processor = processor
        self.deletion_ledger = deletion_ledger
        self.deletion_drain_limit = deletion_drain_limit
        self._tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._started = False
        self.startup_drain_result: VoiceDeletionDrainResult | None = None

    @property
    def active_task_count(self) -> int:
        return len(self._tasks)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.repository.system_recover_interrupted_segments()
            self.startup_drain_result = await asyncio.to_thread(
                self.deletion_ledger.drain,
                limit=self.deletion_drain_limit,
            )
            for pending in self.repository.system_list_pending_segments():
                self.schedule(pending.segment_id, pending.user_id)
        except BaseException:
            await self.stop()
            raise

    def schedule(self, segment_id: int, user_id: int) -> bool:
        if not self._started:
            return False
        key = (segment_id, user_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(
            self.processor.process_initial(segment_id, user_id)
        )
        self._tasks[key] = task
        task.add_done_callback(
            lambda completed, task_key=key: self._task_finished(
                task_key,
                completed,
            )
        )
        return True

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    def _task_finished(
        self,
        key: tuple[int, int],
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            # The transcription service persists controlled failure state. This
            # callback only consumes unexpected task errors so they are not lost
            # as unhandled event-loop exceptions.
            return


def validate_voice_runtime(settings: Settings) -> VoiceRuntimePaths | None:
    """Validate Voice-only prerequisites without contacting the Provider."""

    if not settings.voice_asr_enabled:
        return None

    missing: list[str] = []
    if settings.voice_storage_root is None:
        missing.append("VOICE_STORAGE_ROOT")
    if settings.ffprobe_path is None:
        missing.append("FFPROBE_PATH")
    if settings.ffmpeg_path is None:
        missing.append("FFMPEG_PATH")
    if not settings.alibaba_asr_api_url:
        missing.append("ALIBABA_ASR_API_URL")
    if not settings.alibaba_api_key.get_secret_value():
        missing.append("ALIBABA_API_KEY")
    if missing:
        raise VoiceRuntimeConfigurationError(
            "Voice ASR is enabled but required settings are missing: "
            + ", ".join(missing)
        )

    try:
        endpoint = urlsplit(settings.alibaba_asr_api_url)
    except ValueError as exc:
        raise VoiceRuntimeConfigurationError(
            "ALIBABA_ASR_API_URL must be an HTTPS endpoint without credentials"
        ) from exc
    if (
        endpoint.scheme.lower() != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.fragment
    ):
        raise VoiceRuntimeConfigurationError(
            "ALIBABA_ASR_API_URL must be an HTTPS endpoint without credentials"
        )

    storage_root = _require_absolute_path(
        "VOICE_STORAGE_ROOT",
        settings.voice_storage_root,
    )
    if _is_within(storage_root, PROJECT_ROOT.resolve()):
        raise VoiceRuntimeConfigurationError(
            "VOICE_STORAGE_ROOT must be outside the Git checkout"
        )

    ffprobe_path = _require_executable("FFPROBE_PATH", settings.ffprobe_path)
    ffmpeg_path = _require_executable("FFMPEG_PATH", settings.ffmpeg_path)

    probe_path: Path | None = None
    try:
        for directory in (storage_root, storage_root / "original", storage_root / "tmp"):
            directory.mkdir(parents=True, exist_ok=True)
        probe_path = storage_root / f".selfecho-write-test-{uuid.uuid4().hex}"
        with probe_path.open("xb") as probe_file:
            probe_file.write(b"ok")
            probe_file.flush()
            os.fsync(probe_file.fileno())
    except OSError as exc:
        raise VoiceRuntimeConfigurationError(
            f"VOICE_STORAGE_ROOT is not writable: {type(exc).__name__}"
        ) from exc
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass

    return VoiceRuntimePaths(
        storage_root=storage_root,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
    )


def _require_absolute_path(name: str, value: Path) -> Path:
    if not value.is_absolute():
        raise VoiceRuntimeConfigurationError(f"{name} must be an absolute path")
    return value.resolve()


def _require_executable(name: str, value: Path) -> Path:
    path = _require_absolute_path(name, value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise VoiceRuntimeConfigurationError(
            f"{name} must identify an executable file"
        )
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
