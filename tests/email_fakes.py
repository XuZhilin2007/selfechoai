from __future__ import annotations

from collections import deque

from app.services.tencent_ses import (
    EmailSendOutcome,
    EmailSendResult,
    EmailStatusQueryOutcome,
    EmailStatusResult,
)


class RecordingEmailSender:
    def __init__(
        self,
        *,
        available: bool = True,
        test_email_available: bool = False,
    ) -> None:
        self.available = available
        self.test_email_available = test_email_available
        self.send_results = deque(
            [
                EmailSendResult(
                    EmailSendOutcome.ACCEPTED,
                    provider_message_id="message-1",
                    provider_request_id="request-1",
                    provider_request_date="2026-09-04",
                )
            ]
        )
        self.status_results = deque(
            [EmailStatusResult(EmailStatusQueryOutcome.NOT_FOUND)]
        )
        self.verification_calls: list[tuple[str, str]] = []
        self.reminder_calls: list[tuple[str, str]] = []
        self.test_calls: list[str] = []
        self.status_calls: list[tuple[str, str, str]] = []

    def queue_send_results(self, *results: EmailSendResult) -> None:
        self.send_results.clear()
        self.send_results.extend(results)

    def queue_status_results(self, *results: EmailStatusResult) -> None:
        self.status_results.clear()
        self.status_results.extend(results)

    def _next_send_result(self) -> EmailSendResult:
        if len(self.send_results) > 1:
            return self.send_results.popleft()
        return self.send_results[0]

    def send_verification(self, destination: str, code: str, **_kwargs):
        self.verification_calls.append((destination, code))
        return self._next_send_result()

    def send_reminder(self, destination: str, *, app_url: str, **_kwargs):
        self.reminder_calls.append((destination, app_url))
        return self._next_send_result()

    def send_test(self, destination: str, **_kwargs):
        self.test_calls.append(destination)
        return self._next_send_result()

    def get_send_status(
        self,
        *,
        provider_message_id: str,
        provider_request_date: str,
        destination: str,
    ):
        self.status_calls.append(
            (provider_message_id, provider_request_date, destination)
        )
        if len(self.status_results) > 1:
            return self.status_results.popleft()
        return self.status_results[0]
