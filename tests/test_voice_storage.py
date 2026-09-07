from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import voice_storage
from app.services.voice_storage import (
    EmptyVoiceUploadError,
    InvalidVoiceStorageKeyError,
    VoiceStorage,
    VoiceStorageError,
    VoiceUploadTooLargeError,
)


def test_original_is_streamed_hashed_and_atomically_preserved(tmp_path: Path):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=32)
    payload = b"synthetic-audio"

    saved = storage.store_original([payload[:4], payload[4:]])

    assert saved.size_bytes == len(payload)
    assert saved.sha256 == hashlib.sha256(payload).hexdigest()
    assert saved.path.read_bytes() == payload
    assert re.fullmatch(r"original/[0-9a-f]{2}/[0-9a-f]{32}\.bin", saved.storage_key)
    assert saved.path.is_relative_to(storage.original_root)
    assert list(storage.original_root.rglob("*.part")) == []


def test_original_keys_are_unique_opaque_and_ignore_client_filename(tmp_path: Path):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=32)

    first = storage.store_original([b"first"])
    second = storage.store_original([b"second"])

    assert first.storage_key != second.storage_key
    assert first.path.read_bytes() == b"first"
    assert second.path.read_bytes() == b"second"
    assert "user" not in first.storage_key
    assert "recording" not in first.storage_key


def test_exact_upload_limit_is_accepted_and_actual_overflow_is_rejected(
    tmp_path: Path,
):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=4)

    accepted = storage.store_original([b"12", b"34"])
    with pytest.raises(VoiceUploadTooLargeError):
        storage.store_original([b"123", b"45"])

    assert accepted.path.read_bytes() == b"1234"
    assert [path for path in storage.original_root.rglob("*.bin")] == [accepted.path]
    assert list(storage.original_root.rglob("*.part")) == []


def test_empty_or_invalid_chunks_leave_no_partial_or_reserved_original(
    tmp_path: Path,
):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=8)

    with pytest.raises(EmptyVoiceUploadError):
        storage.store_original([b""])
    with pytest.raises(VoiceStorageError, match="must be bytes"):
        storage.store_original([b"ok", bytearray(b"no")])  # type: ignore[list-item]

    assert list(storage.original_root.rglob("*.part")) == []
    assert list(storage.original_root.rglob("*.bin")) == []


def test_iterable_failure_cleans_partial_and_reserved_files(tmp_path: Path):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=32)

    def failing_chunks():
        yield b"started"
        raise RuntimeError("synthetic stream failure")

    with pytest.raises(RuntimeError, match="synthetic stream failure"):
        storage.store_original(failing_chunks())

    assert list(storage.original_root.rglob("*.part")) == []
    assert list(storage.original_root.rglob("*.bin")) == []


def test_atomic_replace_failure_cleans_partial_and_reserved_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=16)

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(voice_storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        storage.store_original([b"audio"])

    assert list(storage.original_root.rglob("*.part")) == []
    assert list(storage.original_root.rglob("*.bin")) == []


def test_async_stream_uses_same_size_hash_and_atomic_contract(tmp_path: Path):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=6)

    async def chunks():
        yield b"abc"
        yield b"def"

    saved = asyncio.run(storage.store_original_async(chunks()))

    assert saved.path.read_bytes() == b"abcdef"
    assert saved.sha256 == hashlib.sha256(b"abcdef").hexdigest()


@pytest.mark.parametrize(
    "storage_key",
    (
        "../private.wav",
        "original/../private.wav",
        "/original/private.wav",
        "C:/original/private.wav",
        "tmp/private.wav",
        "original\\private.wav",
        "original/./private.wav",
        "",
    ),
)
def test_storage_key_resolution_rejects_traversal_and_non_original_paths(
    tmp_path: Path,
    storage_key: str,
):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=16)

    with pytest.raises(InvalidVoiceStorageKeyError):
        storage.resolve_original(storage_key)


def _backdate(path: Path, age_seconds: int) -> None:
    stale_timestamp = time.time() - age_seconds
    os.utime(path, (stale_timestamp, stale_timestamp))


def test_stale_upload_part_cleanup_is_narrow_and_bounded(tmp_path: Path):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=32)
    directory = storage.original_root / "aa"
    directory.mkdir(parents=True)
    stale_one = directory / f".{('a' * 32)}.part"
    stale_two = directory / f".{('b' * 32)}.part"
    recent = directory / f".{('c' * 32)}.part"
    unmatched = directory / "manual.part"
    for path in (stale_one, stale_two, recent, unmatched):
        path.write_bytes(b"partial")
    for path in (stale_one, stale_two, unmatched):
        _backdate(path, 7200)

    deleted = storage.cleanup_stale_parts(
        older_than=datetime.now(timezone.utc) - timedelta(hours=1),
        limit=1,
    )

    assert deleted == 1
    assert sum(path.exists() for path in (stale_one, stale_two)) == 1
    assert recent.is_file()
    assert unmatched.is_file()


def test_future_tmp_cleanup_is_neutral_and_never_touches_unknown_files(
    tmp_path: Path,
):
    storage = VoiceStorage(tmp_path / "voice", max_upload_bytes=32)
    derived = storage.tmp_root / f"{uuid.uuid4().hex}.wav"
    part = storage.tmp_root / f".{uuid.uuid4().hex}.part.wav"
    unknown = storage.tmp_root / "notes.txt"
    for path in (derived, part, unknown):
        path.write_bytes(b"temporary")
        _backdate(path, 7200)

    deleted = storage.cleanup_stale_tmp_files(
        older_than=datetime.now(timezone.utc) - timedelta(hours=1)
    )

    assert deleted == 2
    assert not derived.exists()
    assert not part.exists()
    assert unknown.is_file()
