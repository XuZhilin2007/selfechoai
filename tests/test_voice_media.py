from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from app.services.voice_media import (
    MAX_PROCESS_DIAGNOSTIC_CHARS,
    MAX_PROBE_OUTPUT_CHARS,
    MediaConversionError,
    MediaMetadata,
    MediaProbeError,
    UnsupportedMediaError,
    VoiceMediaProcessor,
    _run_process_bounded,
)
from app.services.voice_storage import TMP_DERIVED_NAME, TMP_PART_NAME


def probe_payload(
    *,
    container: str = "matroska,webm",
    codec: str = "opus",
    sample_rate: int = 48_000,
    channels: int = 1,
    duration: float = 1.25,
) -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_name": codec,
                    "sample_rate": str(sample_rate),
                    "channels": channels,
                }
            ],
            "format": {"format_name": container, "duration": str(duration)},
        }
    )


def make_processor(tmp_path: Path, runner) -> VoiceMediaProcessor:
    return VoiceMediaProcessor(
        ffprobe_path=Path("C:/tools/ffprobe.exe"),
        ffmpeg_path=Path("C:/tools/ffmpeg.exe"),
        tmp_root=tmp_path / "tmp",
        runner=runner,
    )


def test_probe_uses_actual_metadata_and_only_verified_tuple_is_direct(
    tmp_path: Path,
):
    calls: list[tuple[list[str], dict]] = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, probe_payload(), "")

    processor = make_processor(tmp_path, runner)
    metadata = processor.probe(tmp_path / "opaque-original.bin")

    assert metadata == MediaMetadata(
        container="webm",
        codec="opus",
        sample_rate_hz=48_000,
        channels=1,
        duration_ms=1_250,
        content_type="audio/webm",
    )
    assert processor.is_direct_fast_path(metadata) is True
    arguments, options = calls[0]
    assert arguments[0] == "C:\\tools\\ffprobe.exe"
    assert arguments[-1].endswith("opaque-original.bin")
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    assert options["capture_output"] is True
    assert options["timeout"] == 20.0


@pytest.mark.parametrize(
    "metadata",
    [
        MediaMetadata("ogg", "opus", 48_000, 1, 500, "audio/ogg"),
        MediaMetadata("webm", "vorbis", 48_000, 1, 500, "audio/webm"),
        MediaMetadata("webm", "opus", 16_000, 1, 500, "audio/webm"),
        MediaMetadata("webm", "opus", 48_000, 2, 500, "audio/webm"),
    ],
)
def test_every_unverified_tuple_requires_normalization(metadata: MediaMetadata):
    assert VoiceMediaProcessor.is_direct_fast_path(metadata) is False


def test_direct_path_does_not_invoke_ffmpeg_or_delete_original(tmp_path: Path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"synthetic original")
    processor = make_processor(
        tmp_path,
        lambda *args, **kwargs: pytest.fail("direct path must not run subprocess"),
    )
    metadata = MediaMetadata("webm", "opus", 48_000, 1, 60_000, "audio/webm")

    with processor.prepare_asr_audio(original, metadata) as prepared:
        assert prepared.path == original
        assert prepared.input_kind == "original_direct"
        assert prepared.format == "webm"
        assert prepared.sample_rate_hz is None

    assert original.read_bytes() == b"synthetic original"


def test_non_direct_media_is_normalized_with_fixed_arguments_and_validated(
    tmp_path: Path,
):
    original = tmp_path / "opaque-original.bin"
    original.write_bytes(b"synthetic original")
    calls: list[tuple[list[str], dict]] = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[0].endswith("ffmpeg.exe"):
            Path(arguments[-1]).write_bytes(b"synthetic wav")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(
            arguments,
            0,
            probe_payload(
                container="wav",
                codec="pcm_s16le",
                sample_rate=16_000,
                channels=1,
                duration=1,
            ),
            "",
        )

    processor = make_processor(tmp_path, runner)
    metadata = MediaMetadata("mp4", "aac", 44_100, 2, 1_000, "audio/mp4")

    with processor.prepare_asr_audio(original, metadata) as prepared:
        derived = prepared.path
        assert derived.is_file()
        assert TMP_DERIVED_NAME.fullmatch(derived.name)
        assert prepared.input_kind == "derived_wav"
        assert prepared.format == "wav"
        assert prepared.sample_rate_hz == 16_000

    assert not derived.exists()
    assert original.is_file()
    ffmpeg_arguments, ffmpeg_options = next(
        call for call in calls if call[0][0].endswith("ffmpeg.exe")
    )
    assert ffmpeg_arguments == [
        "C:\\tools\\ffmpeg.exe",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(original),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        ffmpeg_arguments[-1],
    ]
    assert TMP_PART_NAME.fullmatch(Path(ffmpeg_arguments[-1]).name)
    assert ffmpeg_options["shell"] is False


@pytest.mark.parametrize("duration_ms", [60_001, 90_000])
def test_duration_over_sixty_seconds_is_rejected_before_conversion(
    tmp_path: Path,
    duration_ms: int,
):
    processor = make_processor(
        tmp_path,
        lambda *args, **kwargs: pytest.fail("overlong media must not convert"),
    )
    metadata = MediaMetadata("ogg", "vorbis", 44_100, 2, duration_ms, "audio/ogg")

    with pytest.raises(UnsupportedMediaError, match="60 seconds"):
        with processor.prepare_asr_audio(tmp_path / "original", metadata):
            pass


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "{}",
        json.dumps({"streams": [], "format": {}}),
        json.dumps(
            {
                "streams": [{"codec_name": "opus", "channels": 1}],
                "format": {"format_name": "webm", "duration": "1"},
            }
        ),
        json.dumps(
            {
                "streams": [
                    {"codec_name": "opus", "sample_rate": "0", "channels": 1}
                ],
                "format": {"format_name": "webm", "duration": "1"},
            }
        ),
    ],
)
def test_probe_rejects_malformed_or_incomplete_metadata(
    tmp_path: Path,
    stdout: str,
):
    processor = make_processor(
        tmp_path,
        lambda arguments, **kwargs: subprocess.CompletedProcess(
            arguments, 0, stdout, ""
        ),
    )

    with pytest.raises(MediaProbeError):
        processor.probe(tmp_path / "original")


def test_probe_failure_and_timeout_are_controlled_and_diagnostics_bounded(
    tmp_path: Path,
):
    diagnostic = "secret-prefix-" + "x" * 2_000 + "final diagnostic"
    failed = make_processor(
        tmp_path,
        lambda arguments, **kwargs: subprocess.CompletedProcess(
            arguments, 1, "", diagnostic
        ),
    )
    with pytest.raises(MediaProbeError) as captured:
        failed.probe(tmp_path / "original")
    exposed = str(captured.value).removeprefix("ffprobe failed: ")
    assert len(exposed) <= MAX_PROCESS_DIAGNOSTIC_CHARS
    assert exposed.endswith("final diagnostic")
    assert "secret-prefix" not in exposed

    def timeout(arguments, **kwargs):
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

    timed_out = make_processor(tmp_path, timeout)
    with pytest.raises(MediaProbeError, match="ffprobe timed out"):
        timed_out.probe(tmp_path / "original")

    def cannot_start(arguments, **kwargs):
        raise FileNotFoundError("synthetic missing executable")

    unavailable = make_processor(tmp_path, cannot_start)
    with pytest.raises(MediaProbeError, match="FileNotFoundError"):
        unavailable.probe(tmp_path / "original")


@pytest.mark.parametrize(
    "mode",
    ["nonzero", "timeout", "start_error", "missing", "invalid"],
)
def test_conversion_failures_remove_all_temporary_outputs(
    tmp_path: Path,
    mode: str,
):
    original = tmp_path / "original"
    original.write_bytes(b"synthetic")

    def runner(arguments, **kwargs):
        if arguments[0].endswith("ffmpeg.exe"):
            if mode == "start_error":
                raise FileNotFoundError("synthetic missing executable")
            if mode == "timeout":
                Path(arguments[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
            if mode == "nonzero":
                Path(arguments[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(arguments, 1, "", "bad input")
            if mode == "missing":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            Path(arguments[-1]).write_bytes(b"invalid wav")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(
            arguments,
            0,
            probe_payload(container="wav", codec="aac", sample_rate=16_000),
            "",
        )

    processor = make_processor(tmp_path, runner)
    metadata = MediaMetadata("ogg", "vorbis", 44_100, 2, 1_000, "audio/ogg")

    with pytest.raises(MediaConversionError):
        with processor.prepare_asr_audio(original, metadata):
            pass

    assert list((tmp_path / "tmp").glob("*.wav")) == []
    assert original.is_file()


def test_subprocess_timeout_must_be_positive_finite_and_bounded(tmp_path: Path):
    for value in (0, True, float("nan"), float("inf"), 121):
        with pytest.raises(ValueError, match="at most 120"):
            VoiceMediaProcessor(
                ffprobe_path=Path("C:/ffprobe.exe"),
                ffmpeg_path=Path("C:/ffmpeg.exe"),
                tmp_root=tmp_path,
                subprocess_timeout_seconds=value,
            )


def test_default_subprocess_runner_drains_but_retains_bounded_output(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"a" * (MAX_PROBE_OUTPUT_CHARS + 10_000))
            self.stderr = io.BytesIO(
                b"prefix-that-must-drop" + b"b" * (MAX_PROBE_OUTPUT_CHARS + 2_000)
            )

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pytest.fail("successful bounded process must not be killed")

    monkeypatch.setattr(
        "app.services.voice_media.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    completed = _run_process_bounded(
        ["C:/fixed/tool.exe", "--fixed-option"],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert len(completed.stdout) == MAX_PROBE_OUTPUT_CHARS + 1
    assert len(completed.stderr) == MAX_PROBE_OUTPUT_CHARS + 1
    assert "prefix-that-must-drop" not in completed.stderr
