from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.voice_contracts import (
    DIRECT_CHANNELS,
    DIRECT_CODEC,
    DIRECT_CONTAINER_ALIASES,
    DIRECT_SAMPLE_RATE_HZ,
    MAX_VOICE_SEGMENT_DURATION_SECONDS,
)


MAX_PROBE_OUTPUT_CHARS = 64 * 1024
MAX_PROCESS_DIAGNOSTIC_CHARS = 1_000


class VoiceMediaError(RuntimeError):
    failure_code = "internal"


class MediaProbeError(VoiceMediaError):
    failure_code = "media_probe"


class UnsupportedMediaError(VoiceMediaError):
    failure_code = "unsupported_media"


class MediaConversionError(VoiceMediaError):
    failure_code = "conversion"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    container: str
    codec: str
    sample_rate_hz: int
    channels: int
    duration_ms: int
    content_type: str


@dataclass(frozen=True, slots=True)
class PreparedASRAudio:
    path: Path
    input_kind: str
    format: str
    sample_rate_hz: int | None


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


class VoiceMediaProcessor:
    """Inspect actual media and prepare one bounded ASR input."""

    def __init__(
        self,
        *,
        ffprobe_path: Path,
        ffmpeg_path: Path,
        tmp_root: Path,
        subprocess_timeout_seconds: float = 20.0,
        runner: SubprocessRunner | None = None,
    ) -> None:
        if (
            isinstance(subprocess_timeout_seconds, bool)
            or not isinstance(subprocess_timeout_seconds, (int, float))
            or not math.isfinite(subprocess_timeout_seconds)
            or not 0 < subprocess_timeout_seconds <= 120
        ):
            raise ValueError(
                "subprocess_timeout_seconds must be greater than 0 and at most 120"
            )
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.tmp_root = tmp_root.resolve()
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self.subprocess_timeout_seconds = subprocess_timeout_seconds
        self._runner = runner or _run_process_bounded

    def probe(self, path: Path) -> MediaMetadata:
        completed = self._run(
            [
                str(self.ffprobe_path),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "format=format_name,duration:stream=codec_name,sample_rate,channels,duration",
                "-of",
                "json",
                str(path),
            ],
            error_type=MediaProbeError,
            operation="ffprobe",
        )
        if not isinstance(completed.stdout, str):
            raise MediaProbeError("ffprobe returned invalid media metadata")
        if len(completed.stdout) > MAX_PROBE_OUTPUT_CHARS:
            raise MediaProbeError("ffprobe returned oversized media metadata")
        try:
            payload = json.loads(completed.stdout)
            streams = payload.get("streams")
            if not isinstance(streams, list) or not streams:
                raise ValueError("missing audio stream")
            stream = streams[0]
            if not isinstance(stream, dict):
                raise ValueError("invalid audio stream")
            format_data = payload.get("format")
            if not isinstance(format_data, dict):
                raise ValueError("missing format")
            container = _normalize_container(str(format_data["format_name"]))
            codec = str(stream["codec_name"]).strip().lower()
            sample_rate_hz = int(stream["sample_rate"])
            channels = int(stream["channels"])
            duration_value = format_data.get("duration", stream.get("duration"))
            duration_ms = int(round(float(duration_value) * 1000))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise MediaProbeError("ffprobe returned invalid media metadata") from exc
        if not container or not codec:
            raise MediaProbeError("ffprobe returned incomplete media metadata")
        if sample_rate_hz <= 0 or channels <= 0 or duration_ms <= 0:
            raise MediaProbeError("ffprobe returned non-positive media metadata")
        return MediaMetadata(
            container=container,
            codec=codec,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            duration_ms=duration_ms,
            content_type=content_type_for_container(container),
        )

    @staticmethod
    def is_direct_fast_path(metadata: MediaMetadata) -> bool:
        return (
            metadata.container in DIRECT_CONTAINER_ALIASES
            and metadata.codec == DIRECT_CODEC
            and metadata.sample_rate_hz == DIRECT_SAMPLE_RATE_HZ
            and metadata.channels == DIRECT_CHANNELS
            and metadata.duration_ms
            <= int(MAX_VOICE_SEGMENT_DURATION_SECONDS * 1000)
        )

    @contextmanager
    def prepare_asr_audio(
        self,
        original_path: Path,
        metadata: MediaMetadata,
    ) -> Iterator[PreparedASRAudio]:
        self._validate_duration(metadata)
        if self.is_direct_fast_path(metadata):
            yield PreparedASRAudio(
                path=original_path,
                input_kind="original_direct",
                format="webm",
                sample_rate_hz=None,
            )
            return

        token = uuid.uuid4().hex
        derived_path = self.tmp_root / f"{token}.wav"
        part_path = self.tmp_root / f".{token}.part.wav"
        try:
            self._run(
                [
                    str(self.ffmpeg_path),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(original_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(part_path),
                ],
                error_type=MediaConversionError,
                operation="ffmpeg",
            )
            try:
                if not part_path.is_file() or part_path.stat().st_size <= 0:
                    raise MediaConversionError("ffmpeg produced no WAV output")
                os.replace(part_path, derived_path)
            except OSError as exc:
                raise MediaConversionError(
                    f"ffmpeg output could not be finalized: {type(exc).__name__}"
                ) from exc
            converted = self.probe(derived_path)
            if not (
                converted.container == "wav"
                and converted.codec == "pcm_s16le"
                and converted.sample_rate_hz == 16_000
                and converted.channels == 1
            ):
                raise MediaConversionError(
                    "converted media is not mono 16 kHz PCM s16le WAV"
                )
            self._validate_duration(converted)
            yield PreparedASRAudio(
                path=derived_path,
                input_kind="derived_wav",
                format="wav",
                sample_rate_hz=16_000,
            )
        except MediaProbeError as exc:
            raise MediaConversionError(
                "converted WAV could not be validated"
            ) from exc
        finally:
            for path in (part_path, derived_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _validate_duration(self, metadata: MediaMetadata) -> None:
        maximum_ms = int(MAX_VOICE_SEGMENT_DURATION_SECONDS * 1000)
        if metadata.duration_ms > maximum_ms:
            raise UnsupportedMediaError("Voice Segment exceeds 60 seconds")

    def _run(
        self,
        arguments: Sequence[str],
        *,
        error_type: type[VoiceMediaError],
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(
                list(arguments),
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.subprocess_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise error_type(f"{operation} timed out") from exc
        except OSError as exc:
            raise error_type(
                f"{operation} could not start: {type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            diagnostic = (completed.stderr or "no diagnostic").strip()
            diagnostic = diagnostic[-MAX_PROCESS_DIAGNOSTIC_CHARS:]
            raise error_type(f"{operation} failed: {diagnostic}")
        return completed


def _run_process_bounded(
    arguments: list[str],
    *,
    shell: bool,
    stdin,
    capture_output: bool,
    text: bool,
    timeout: float,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed command while draining but not retaining unbounded output."""

    if shell or stdin is not subprocess.DEVNULL or not capture_output or not text:
        raise ValueError("invalid bounded subprocess options")
    retained_limit = MAX_PROBE_OUTPUT_CHARS + 1
    process = subprocess.Popen(
        arguments,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream, retained: bytearray, *, keep_tail: bool) -> None:
        try:
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    return
                if keep_tail:
                    retained.extend(chunk)
                    if len(retained) > retained_limit:
                        del retained[:-retained_limit]
                elif len(retained) < retained_limit:
                    retained.extend(chunk[: retained_limit - len(retained)])
        finally:
            stream.close()

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout),
        kwargs={"keep_tail": False},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr),
        kwargs={"keep_tail": True},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=bytes(stdout),
            stderr=bytes(stderr),
        )
    stdout_thread.join()
    stderr_thread.join()
    completed = subprocess.CompletedProcess(
        arguments,
        returncode,
        bytes(stdout).decode("utf-8", errors="replace"),
        bytes(stderr).decode("utf-8", errors="replace"),
    )
    if check and returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            arguments,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _normalize_container(raw_value: str) -> str:
    names = {name.strip().lower() for name in raw_value.split(",") if name.strip()}
    if names & DIRECT_CONTAINER_ALIASES:
        return "webm"
    if "wav" in names:
        return "wav"
    if "ogg" in names:
        return "ogg"
    if "mp3" in names:
        return "mp3"
    if "mov" in names or "mp4" in names:
        return "mp4"
    return sorted(names)[0] if names else ""


def content_type_for_container(container: str | None) -> str:
    return {
        "webm": "audio/webm",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "mp4": "audio/mp4",
    }.get(container, "application/octet-stream")
