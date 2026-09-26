from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.email_admission import EmailAdmissionDenied, EmailSendAdmission


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        database_path=tmp_path / "email-admission.db",
        email_verification_recipient_window_seconds=10,
        email_verification_recipient_limit=2,
        email_verification_recipient_max_keys=1,
        email_send_window_seconds=10,
        email_send_global_limit=3,
        **overrides,
    )


def test_reservations_are_atomic_and_unconsumed_work_does_not_use_quota(tmp_path):
    clock = Clock()
    admission = EmailSendAdmission(settings(tmp_path), clock=clock)
    with admission.reserve_verification(" One@Example.com "):
        with admission.reserve_verification("one@example.com"):
            with pytest.raises(EmailAdmissionDenied):
                admission.reserve_verification("ONE@example.com")
        with admission.reserve_verification("one@example.com") as sent:
            sent.consume()
    with admission.reserve_verification("one@example.com") as sent:
        sent.consume()
    with pytest.raises(EmailAdmissionDenied):
        admission.reserve_verification("one@example.com")
    assert len(admission._recipients) == 1

    with pytest.raises(EmailAdmissionDenied):
        admission.reserve_verification("different@example.com")
    clock.now = 11
    with admission.reserve_verification("different@example.com") as sent:
        sent.consume()
    assert len(admission._recipients) == 1
    with EmailSendAdmission(settings(tmp_path), clock=clock).reserve_verification(
        "one@example.com"
    ) as restarted:
        restarted.consume()


def test_global_reservations_release_before_send_and_failures_stay_counted(tmp_path):
    clock = Clock()
    admission = EmailSendAdmission(settings(tmp_path), clock=clock)
    first = admission.reserve_send()
    second = admission.reserve_send()
    third = admission.reserve_send()
    with pytest.raises(EmailAdmissionDenied):
        admission.reserve_send()
    first.release()
    with admission.reserve_send() as sent:
        sent.consume()
    second.consume()
    second.release()
    third.release()
    with admission.reserve_send() as sent:
        sent.consume()
    with pytest.raises(EmailAdmissionDenied):
        admission.reserve_send()
    clock.now = 11
    with admission.reserve_send():
        pass


@pytest.mark.parametrize("name", [
    "EMAIL_VERIFICATION_RECIPIENT_WINDOW_SECONDS",
    "EMAIL_VERIFICATION_RECIPIENT_LIMIT",
    "EMAIL_VERIFICATION_RECIPIENT_MAX_KEYS",
    "EMAIL_SEND_WINDOW_SECONDS",
    "EMAIL_SEND_GLOBAL_LIMIT",
])
@pytest.mark.parametrize("value", ["0", "invalid"])
def test_email_admission_configuration_fails_clearly(
    monkeypatch, tmp_path: Path, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_environment(tmp_path / "empty.env")
