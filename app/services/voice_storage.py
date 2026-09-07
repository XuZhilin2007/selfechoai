from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath


SERVER_PART_NAME = re.compile(r"^\.[0-9a-f]{32}\.part$")
# Stage 1 does not create conversion files. These exact, narrow patterns make
# the future tmp namespace cleanable without ever reclaiming arbitrary files.
TMP_DERIVED_NAME = re.compile(r"^[0-9a-f]{32}\.wav$")
TMP_PART_NAME = re.compile(r"^\.[0-9a-f]{32}\.part\.wav$")


class VoiceStorageError(RuntimeError):
    pass


class EmptyVoiceUploadError(VoiceStorageError):
    pass


class VoiceUploadTooLargeError(VoiceStorageError):
    pass


class InvalidVoiceStorageKeyError(VoiceStorageError):
    pass


@dataclass(frozen=True, slots=True)
class StoredOriginal:
    storage_key: str
    size_bytes: int
    sha256: str
    path: Path


class _OriginalWriter:
    def __init__(self, storage: "VoiceStorage", storage_key: str) -> None:
        token = uuid.uuid4().hex
        self.storage = storage
        self.storage_key = storage_key
        self.final_path = storage.resolve_original(storage_key)
        self.part_path = self.final_path.with_name(f".{token}.part")
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.final_path.open("xb"):
                pass
        except FileExistsError as exc:
            raise VoiceStorageError("Original Audio storage key already exists") from exc
        self.reserved = True
        try:
            self.file = self.part_path.open("xb")
        except Exception:
            self.final_path.unlink(missing_ok=True)
            raise
        self.digest = hashlib.sha256()
        self.size = 0
        self.finished = False

    def write(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise VoiceStorageError("voice upload chunks must be bytes")
        if not chunk:
            return
        next_size = self.size + len(chunk)
        if next_size > self.storage.max_upload_bytes:
            raise VoiceUploadTooLargeError(
                f"voice upload exceeds {self.storage.max_upload_bytes} bytes"
            )
        self.file.write(chunk)
        self.digest.update(chunk)
        self.size = next_size

    def finish(self) -> StoredOriginal:
        if self.size == 0:
            raise EmptyVoiceUploadError("voice upload body is empty")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        os.replace(self.part_path, self.final_path)
        _fsync_directory(self.final_path.parent)
        self.finished = True
        return StoredOriginal(
            storage_key=self.storage_key,
            size_bytes=self.size,
            sha256=self.digest.hexdigest(),
            path=self.final_path,
        )

    def abort(self) -> None:
        if not self.file.closed:
            self.file.close()
        if self.finished:
            return
        self.part_path.unlink(missing_ok=True)
        if self.reserved:
            self.final_path.unlink(missing_ok=True)


class VoiceStorage:
    """Immutable Original Audio storage below an injected external root."""

    def __init__(self, root: Path, max_upload_bytes: int) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.root = root.resolve()
        self.original_root = self.root / "original"
        self.tmp_root = self.root / "tmp"
        self.max_upload_bytes = max_upload_bytes
        self.original_root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def allocate_original_key(self) -> str:
        token = uuid.uuid4().hex
        return f"original/{token[:2]}/{token}.bin"

    def store_original(
        self,
        chunks: Iterable[bytes],
        *,
        storage_key: str | None = None,
    ) -> StoredOriginal:
        writer = _OriginalWriter(self, storage_key or self.allocate_original_key())
        try:
            for chunk in chunks:
                writer.write(chunk)
            return writer.finish()
        finally:
            writer.abort()

    async def store_original_async(
        self,
        chunks: AsyncIterable[bytes],
        *,
        storage_key: str | None = None,
    ) -> StoredOriginal:
        writer = _OriginalWriter(self, storage_key or self.allocate_original_key())
        try:
            async for chunk in chunks:
                writer.write(chunk)
            return writer.finish()
        finally:
            writer.abort()

    def resolve_original(self, storage_key: str) -> Path:
        if not isinstance(storage_key, str):
            raise InvalidVoiceStorageKeyError("invalid Voice storage key")
        raw_parts = storage_key.split("/")
        if (
            "\\" in storage_key
            or not raw_parts
            or raw_parts[0] != "original"
            or any(part in {"", ".", ".."} for part in raw_parts)
        ):
            raise InvalidVoiceStorageKeyError("invalid Original Audio storage key")
        try:
            key = PurePosixPath(storage_key)
        except (TypeError, ValueError) as exc:
            raise InvalidVoiceStorageKeyError("invalid Voice storage key") from exc
        if (
            key.is_absolute()
            or len(key.parts) < 2
            or key.parts[0] != "original"
        ):
            raise InvalidVoiceStorageKeyError("invalid Original Audio storage key")
        candidate = (self.root / Path(*key.parts)).resolve()
        try:
            candidate.relative_to(self.original_root)
        except ValueError as exc:
            raise InvalidVoiceStorageKeyError(
                "Original Audio path escapes storage root"
            ) from exc
        return candidate

    def delete_original(self, storage_key: str) -> bool:
        path = self.resolve_original(storage_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(path.parent)
        return True

    def cleanup_stale_parts(
        self,
        *,
        older_than: datetime,
        limit: int = 25,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff_timestamp = older_than.timestamp()
        original_root = self.original_root.resolve()
        deleted = 0
        examined = 0
        for candidate in self.original_root.rglob("*.part"):
            if examined >= limit:
                break
            examined += 1
            if not SERVER_PART_NAME.fullmatch(candidate.name):
                continue
            try:
                candidate.resolve(strict=True).relative_to(original_root)
                if candidate.stat().st_mtime > cutoff_timestamp:
                    continue
                candidate.unlink()
            except (FileNotFoundError, OSError, ValueError):
                continue
            deleted += 1
        return deleted

    def cleanup_stale_tmp_files(
        self,
        *,
        older_than: datetime,
        limit: int = 25,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff_timestamp = older_than.timestamp()
        tmp_root = self.tmp_root.resolve()
        deleted = 0
        examined = 0
        for candidate in self.tmp_root.iterdir():
            if examined >= limit:
                break
            examined += 1
            if not (
                TMP_DERIVED_NAME.fullmatch(candidate.name)
                or TMP_PART_NAME.fullmatch(candidate.name)
            ):
                continue
            try:
                candidate.resolve(strict=True).relative_to(tmp_root)
                if candidate.stat().st_mtime > cutoff_timestamp:
                    continue
                candidate.unlink()
            except (FileNotFoundError, OSError, ValueError):
                continue
            deleted += 1
        return deleted


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
