from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.auth import normalize_email
from app.config import Settings


class AdmissionDenied(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


def client_source(request: Request) -> str:
    """Use the ASGI server's client identity, never a request header directly."""

    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Missing or malformed server identity shares one limited bucket.
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        # A zone suffix is local interface metadata, not a distinct client.
        address = ipaddress.IPv6Address(int(address))
        if address.ipv4_mapped:
            address = address.ipv4_mapped
    return address.compressed


@dataclass(slots=True)
class _Window:
    started: float
    count: int


class _Counter:
    def __init__(self, limit: int, window_seconds: int, max_keys: int) -> None:
        if min(limit, window_seconds, max_keys) <= 0:
            raise ValueError("auth admission limits, windows, and max keys must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self.entries: dict[str, _Window] = {}

    def prune(self, now: float) -> None:
        for key, entry in list(self.entries.items()):
            if now - entry.started >= self.window_seconds:
                del self.entries[key]

    def retry_after(self, key: str, now: float) -> int:
        entry = self.entries.get(key)
        if entry is not None and now - entry.started >= self.window_seconds:
            del self.entries[key]
            entry = None
        if entry is not None:
            if entry.count >= self.limit:
                return max(1, math.ceil(entry.started + self.window_seconds - now))
            return 0
        if len(self.entries) >= self.max_keys:
            self.prune(now)
            if len(self.entries) >= self.max_keys:
                earliest = min(
                    item.started + self.window_seconds for item in self.entries.values()
                )
                return max(1, math.ceil(earliest - now))
        return 0

    def take(self, key: str, now: float) -> None:
        entry = self.entries.get(key)
        if entry is None:
            self.entries[key] = _Window(now, 1)
        else:
            entry.count += 1


class AuthAdmissionLimiter:
    """Atomic in-process admission before any auth service work."""

    def __init__(
        self, settings: Settings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        max_keys = settings.auth_admission_max_keys
        self._registration_source = _Counter(
            settings.auth_registration_source_limit,
            settings.auth_registration_window_seconds,
            max_keys,
        )
        self._registration_global = _Counter(
            settings.auth_registration_global_limit,
            settings.auth_registration_window_seconds,
            1,
        )
        self._login_source = _Counter(
            settings.auth_login_source_limit,
            settings.auth_login_window_seconds,
            max_keys,
        )
        self._login_account = _Counter(
            settings.auth_login_account_limit,
            settings.auth_login_window_seconds,
            max_keys,
        )
        self._login_global = _Counter(
            settings.auth_login_global_limit,
            settings.auth_login_window_seconds,
            1,
        )
        self._counters = (
            self._registration_source,
            self._registration_global,
            self._login_source,
            self._login_account,
            self._login_global,
        )
        self._next_cleanup = 0.0
        self._cleanup_interval = min(
            settings.auth_registration_window_seconds,
            settings.auth_login_window_seconds,
        )

    def admit_registration(self, source: str) -> None:
        self._admit(
            ((self._registration_source, source), (self._registration_global, "*"))
        )

    def admit_login(self, source: str, email: str) -> None:
        self._admit(
            (
                (self._login_source, source),
                (self._login_account, normalize_email(email)),
                (self._login_global, "*"),
            )
        )

    def _admit(self, checks: tuple[tuple[_Counter, str], ...]) -> None:
        with self._lock:
            now = self._clock()
            if now >= self._next_cleanup:
                for counter in self._counters:
                    counter.prune(now)
                self._next_cleanup = now + self._cleanup_interval
            retry_after = max(counter.retry_after(key, now) for counter, key in checks)
            if retry_after:
                raise AdmissionDenied(retry_after)
            for counter, key in checks:
                counter.take(key, now)
