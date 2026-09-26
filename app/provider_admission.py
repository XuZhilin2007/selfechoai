"""Bounded, process-local admission for outbound AI and ASR work."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from app.config import Settings


class ProviderAdmissionDenied(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class ProviderPermit:
    def __init__(self, limiter: GlobalProviderLimiter) -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        with self._limiter._lock:
            if not self._released:
                self._released = True
                self._limiter._active -= 1

    def __enter__(self) -> ProviderPermit:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class GlobalProviderLimiter:
    """One fixed-window call counter plus an independent in-flight ceiling."""

    def __init__(
        self,
        window_seconds: int,
        call_limit: int,
        concurrent_limit: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(window_seconds, call_limit, concurrent_limit) <= 0:
            raise ValueError("provider admission window and limits must be positive")
        self.window_seconds = window_seconds
        self.call_limit = call_limit
        self.concurrent_limit = concurrent_limit
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start: float | None = None
        self._used = 0
        self._active = 0

    def acquire(self) -> ProviderPermit:
        with self._lock:
            now = self._clock()
            if self._window_start is not None and now - self._window_start >= self.window_seconds:
                self._window_start = None
                self._used = 0
            if self._window_start is not None and self._used >= self.call_limit:
                raise ProviderAdmissionDenied(
                    max(1, math.ceil(self._window_start + self.window_seconds - now))
                )
            if self._active >= self.concurrent_limit:
                raise ProviderAdmissionDenied(1)
            if self._window_start is None:
                self._window_start = now
            self._used += 1
            self._active += 1
            return ProviderPermit(self)


class ProviderAdmission:
    def __init__(
        self, settings: Settings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if settings.ai_provider_input_max_bytes <= 0:
            raise ValueError("AI_PROVIDER_INPUT_MAX_BYTES must be positive")
        self.ai = GlobalProviderLimiter(
            settings.ai_admission_window_seconds,
            settings.ai_admission_call_limit,
            settings.ai_admission_concurrent_limit,
            clock=clock,
        )
        self.asr = GlobalProviderLimiter(
            settings.asr_admission_window_seconds,
            settings.asr_admission_call_limit,
            settings.asr_admission_concurrent_limit,
            clock=clock,
        )
