"""Bounded, process-local admission for outbound email sends."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.email_repository import normalize_reminder_email


class EmailAdmissionDenied(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


@dataclass(slots=True)
class _Window:
    started: float
    count: int = 0


class EmailSendLease:
    def __init__(
        self,
        admission: "EmailSendAdmission",
        global_window: _Window,
        recipient: str | None,
        recipient_window: _Window | None,
    ) -> None:
        self._admission = admission
        self._global_window = global_window
        self._recipient = recipient
        self._recipient_window = recipient_window
        self._consumed = False
        self._released = False

    def consume(self) -> None:
        """Count the send immediately before the provider call, even on failure."""
        with self._admission._lock:
            if self._released or self._consumed:
                raise RuntimeError("email send lease is no longer available")
            self._consumed = True

    def release(self) -> None:
        with self._admission._lock:
            if self._released:
                return
            self._released = True
            if self._consumed:
                return
            self._global_window.count -= 1
            if (
                self._admission._global is self._global_window
                and self._global_window.count == 0
            ):
                self._admission._global = None
            if self._recipient_window is not None:
                self._recipient_window.count -= 1
                if (
                    self._admission._recipients.get(self._recipient)
                    is self._recipient_window
                    and self._recipient_window.count == 0
                ):
                    del self._admission._recipients[self._recipient]

    def __enter__(self) -> EmailSendLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class EmailSendAdmission:
    """Reserve before local state changes; consume only at the send boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.global_window_seconds = settings.email_send_window_seconds
        self.global_limit = settings.email_send_global_limit
        self.recipient_window_seconds = settings.email_verification_recipient_window_seconds
        self.recipient_limit = settings.email_verification_recipient_limit
        self.max_recipients = settings.email_verification_recipient_max_keys
        if min(
            self.global_window_seconds,
            self.global_limit,
            self.recipient_window_seconds,
            self.recipient_limit,
            self.max_recipients,
        ) <= 0:
            raise ValueError("email admission settings must be positive")
        self._clock = clock
        self._lock = threading.Lock()
        self._global: _Window | None = None
        self._recipients: dict[str, _Window] = {}

    def reserve_send(self) -> EmailSendLease:
        return self._reserve(None)

    def reserve_verification(self, destination: str) -> EmailSendLease:
        return self._reserve(normalize_reminder_email(destination))

    def _reserve(self, recipient: str | None) -> EmailSendLease:
        with self._lock:
            now = self._clock()
            global_window = self._global
            if (
                global_window is None
                or now - global_window.started >= self.global_window_seconds
            ):
                global_window = _Window(now)
                self._global = global_window
            global_retry = (
                max(1, math.ceil(global_window.started + self.global_window_seconds - now))
                if global_window.count >= self.global_limit else 0
            )

            recipient_window = None
            recipient_retry = 0
            if recipient is not None:
                recipient_window = self._recipients.get(recipient)
                if recipient_window is not None and (
                    now - recipient_window.started >= self.recipient_window_seconds
                ):
                    del self._recipients[recipient]
                    recipient_window = None
                if recipient_window is None and len(self._recipients) >= self.max_recipients:
                    self._recipients = {
                        key: window for key, window in self._recipients.items()
                        if now - window.started < self.recipient_window_seconds
                    }
                    if len(self._recipients) >= self.max_recipients:
                        recipient_retry = max(1, math.ceil(min(
                            window.started + self.recipient_window_seconds
                            for window in self._recipients.values()
                        ) - now))
                if recipient_window is not None and recipient_window.count >= self.recipient_limit:
                    recipient_retry = max(
                        recipient_retry,
                        max(1, math.ceil(
                            recipient_window.started + self.recipient_window_seconds - now
                        )),
                    )
            retry_after = max(global_retry, recipient_retry)
            if retry_after:
                raise EmailAdmissionDenied(retry_after)
            if recipient is not None and recipient_window is None:
                recipient_window = _Window(now)
                self._recipients[recipient] = recipient_window
            global_window.count += 1
            if recipient_window is not None:
                recipient_window.count += 1
            return EmailSendLease(self, global_window, recipient, recipient_window)
