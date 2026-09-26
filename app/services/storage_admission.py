"""Process-local admission for new persistent content and Original Audio."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path


# A bounded SQLite write can allocate new WAL/database pages as well as content.
DATABASE_WRITE_ALLOWANCE = 1024 * 1024


class StorageAdmissionDenied(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _VoiceSlot:
    def __init__(self, bytes_by_device: dict[int, int]) -> None:
        self.bytes_by_device = bytes_by_device
        self.references = 1


class VoiceStorageLease:
    def __init__(self, admission: "StorageAdmission", slot: _VoiceSlot) -> None:
        self._admission = admission
        self._slot = slot
        self._released = False

    def fork(self) -> "VoiceStorageLease":
        return self._admission._fork(self)

    def release(self) -> None:
        self._admission._release(self)

    def __enter__(self) -> "VoiceStorageLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class DatabaseStorageLease:
    def __init__(self, admission: "StorageAdmission", device: int, size: int) -> None:
        self._admission = admission
        self._device = device
        self._size = size
        self._released = False

    def release(self) -> None:
        with self._admission._lock:
            if self._released:
                return
            self._released = True
            remaining = self._admission._reserved_by_device[self._device] - self._size
            if remaining:
                self._admission._reserved_by_device[self._device] = remaining
            else:
                del self._admission._reserved_by_device[self._device]

    def __enter__(self) -> DatabaseStorageLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class StorageAdmission:
    """One bounded write window, with full-size reservations for Voice files.

    Reservations are counted on each configured path's actual filesystem. The
    allowance for SQLite metadata is included before a Voice session begins.
    """

    def __init__(
        self,
        database_path: Path,
        voice_root: Path | None,
        *,
        min_free_bytes: int,
        write_window_seconds: int,
        write_limit: int,
        voice_concurrent_limit: int,
        voice_max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(min_free_bytes, write_window_seconds, write_limit,
               voice_concurrent_limit, voice_max_bytes) <= 0:
            raise ValueError("storage admission settings must be positive")
        self.database_path = database_path.resolve()
        self.voice_root = voice_root.resolve() if voice_root is not None else None
        self.min_free_bytes = min_free_bytes
        self.write_window_seconds = write_window_seconds
        self.write_limit = write_limit
        self.voice_concurrent_limit = voice_concurrent_limit
        self.voice_max_bytes = voice_max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start: float | None = None
        self._used = 0
        self._active_voice = 0
        self._reserved_by_device: dict[int, int] = {}

    @staticmethod
    def _filesystem(path: Path) -> tuple[int, int]:
        candidate = path
        while not candidate.exists():
            if candidate.parent == candidate:
                raise StorageAdmissionDenied("capacity")
            candidate = candidate.parent
        try:
            return os.stat(candidate).st_dev, shutil.disk_usage(candidate).free
        except OSError as exc:
            raise StorageAdmissionDenied("capacity") from exc

    def _admit_write_locked(self, now: float) -> None:
        if (self._window_start is not None
                and now - self._window_start >= self.write_window_seconds):
            self._window_start = None
            self._used = 0
        if self._used >= self.write_limit:
            raise StorageAdmissionDenied("pressure")
        if self._window_start is None:
            self._window_start = now
        self._used += 1

    def reserve_database_growth(self, estimated_growth_bytes: int = 0) -> DatabaseStorageLease:
        if estimated_growth_bytes < 0:
            raise ValueError("estimated database growth must not be negative")
        with self._lock:
            now = self._clock()
            device, free = self._filesystem(self.database_path.parent)
            size = DATABASE_WRITE_ALLOWANCE + estimated_growth_bytes
            if (free - self._reserved_by_device.get(device, 0)
                    < self.min_free_bytes + size):
                raise StorageAdmissionDenied("capacity")
            self._admit_write_locked(now)
            self._reserved_by_device[device] = (
                self._reserved_by_device.get(device, 0) + size
            )
            return DatabaseStorageLease(self, device, size)

    def reserve_voice(self) -> VoiceStorageLease:
        if self.voice_root is None:
            raise StorageAdmissionDenied("capacity")
        with self._lock:
            now = self._clock()
            if self._active_voice >= self.voice_concurrent_limit:
                raise StorageAdmissionDenied("pressure")
            db_device, db_free = self._filesystem(self.database_path.parent)
            voice_device, voice_free = self._filesystem(self.voice_root)
            needed = {db_device: DATABASE_WRITE_ALLOWANCE}
            needed[voice_device] = needed.get(voice_device, 0) + self.voice_max_bytes
            available = {db_device: db_free, voice_device: voice_free}
            if db_device == voice_device:
                available[db_device] = min(db_free, voice_free)
            for device, amount in needed.items():
                if (available[device] - self._reserved_by_device.get(device, 0)
                        < self.min_free_bytes + amount):
                    raise StorageAdmissionDenied("capacity")
            self._admit_write_locked(now)
            slot = _VoiceSlot(needed)
            self._active_voice += 1
            for device, amount in needed.items():
                self._reserved_by_device[device] = (
                    self._reserved_by_device.get(device, 0) + amount
                )
            return VoiceStorageLease(self, slot)

    def _fork(self, lease: VoiceStorageLease) -> VoiceStorageLease:
        with self._lock:
            if lease._released:
                raise ValueError("Voice storage reservation is no longer active")
            lease._slot.references += 1
            return VoiceStorageLease(self, lease._slot)

    def _release_locked(self, lease: VoiceStorageLease) -> None:
        if lease._released:
            return
        lease._released = True
        slot = lease._slot
        slot.references -= 1
        if slot.references == 0:
            self._active_voice -= 1
            for device, amount in slot.bytes_by_device.items():
                remaining = self._reserved_by_device[device] - amount
                if remaining:
                    self._reserved_by_device[device] = remaining
                else:
                    del self._reserved_by_device[device]

    def _release(self, lease: VoiceStorageLease) -> None:
        with self._lock:
            self._release_locked(lease)
