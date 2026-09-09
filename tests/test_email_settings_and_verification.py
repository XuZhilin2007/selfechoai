from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth_repository import AuthRepository
from app.database import Database
from app.email_repository import (
    EmailAddressPausedError,
    EmailRateLimitError,
    EmailReminderRepository,
    EmailVerificationError,
)
from app.schemas import EmailPauseReason
from app.services.email_reminders import verification_code_hmac
from app.services.tencent_ses import EmailSendOutcome, EmailSendResult


NOW = datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc)
PEPPER = "test-only-pepper"


@pytest.fixture
def email_context(tmp_path):
    database = Database(tmp_path / "email-settings.db")
    database.initialize()
    auth = AuthRepository(database)
    first = auth.create_user(
        email="login@example.com",
        password_hash="hash",
        display_name="First",
        timezone_name="Asia/Shanghai",
    )
    second = auth.create_user(
        email="second-login@example.com",
        password_hash="hash",
        display_name="Second",
        timezone_name="Asia/Shanghai",
    )
    return database, EmailReminderRepository(database), first, second


def accepted_result(index=1):
    return EmailSendResult(
        EmailSendOutcome.ACCEPTED,
        provider_message_id=f"message-{index}",
        provider_request_id=f"request-{index}",
        provider_request_date="2026-09-04",
    )


def create_sent_challenge(repository, user_id, address, code, when=NOW):
    challenge = repository.create_verification_challenge(
        user_id,
        email_address=address,
        code_hmac=verification_code_hmac(PEPPER, user_id, address, code),
        created_at=when,
    )
    repository.finish_verification_send(
        challenge.id,
        user_id,
        accepted_result(challenge.id),
    )
    return challenge


def test_settings_are_independent_from_login_email_and_user_scoped(email_context):
    _database, repository, first, second = email_context
    assert repository.get_settings(first.id).email_address is None

    first_settings = repository.set_candidate_address(
        first.id, "  Reminder@Example.COM  ", changed_at=NOW
    )

    assert first_settings.email_address == "reminder@example.com"
    assert first_settings.verification_status.value == "pending"
    assert repository.get_settings(second.id).email_address is None
    assert first_settings.email_address != first.email

    second_settings = repository.set_candidate_address(
        second.id, "reminder@example.com", changed_at=NOW
    )
    assert second_settings.email_address == first_settings.email_address
    assert second_settings.user_id == second.id


def test_enabled_intent_survives_address_change_but_effective_state_is_pending(
    email_context,
):
    _database, repository, first, _second = email_context
    repository.set_enabled(first.id, True, changed_at=NOW)
    first_address = repository.set_candidate_address(
        first.id, "first@example.com", changed_at=NOW
    )
    assert first_address.enabled is True
    assert first_address.effective_active is False

    create_sent_challenge(repository, first.id, "first@example.com", "123456")
    verified = repository.confirm_verification(
        first.id,
        email_address="first@example.com",
        submitted_hmac=verification_code_hmac(
            PEPPER, first.id, "first@example.com", "123456"
        ),
        verified_at=NOW + timedelta(seconds=1),
    )
    assert verified.effective_active is True

    changed = repository.set_candidate_address(
        first.id,
        "second@example.com",
        changed_at=NOW + timedelta(minutes=1),
    )
    assert changed.enabled is True
    assert changed.verification_status.value == "pending"
    assert changed.effective_active is False


def test_verification_persists_only_hmac_and_consumes_code_once(email_context):
    database, repository, first, _second = email_context
    repository.set_candidate_address(first.id, "verify@example.com", changed_at=NOW)
    create_sent_challenge(repository, first.id, "verify@example.com", "012345")
    with database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM email_verification_challenges"
        ).fetchone()
    assert row["code_hmac"] != "012345"
    assert len(row["code_hmac"]) == 64
    assert "012345" not in tuple(row)

    settings = repository.confirm_verification(
        first.id,
        email_address="verify@example.com",
        submitted_hmac=verification_code_hmac(
            PEPPER, first.id, "verify@example.com", "012345"
        ),
        verified_at=NOW + timedelta(minutes=1),
    )
    assert settings.verification_status.value == "verified"
    with pytest.raises(EmailVerificationError):
        repository.confirm_verification(
            first.id,
            email_address="verify@example.com",
            submitted_hmac=verification_code_hmac(
                PEPPER, first.id, "verify@example.com", "012345"
            ),
            verified_at=NOW + timedelta(minutes=2),
        )


@pytest.mark.parametrize(
    ("send_outcome", "confirmation_allowed"),
    [
        (EmailSendOutcome.ACCEPTED, True),
        (EmailSendOutcome.AMBIGUOUS_FAILURE, True),
        (None, False),
        (EmailSendOutcome.PERMANENT_FAILURE, False),
    ],
)
def test_confirmation_accepts_only_accepted_or_unknown_send_status(
    email_context,
    send_outcome,
    confirmation_allowed,
):
    _database, repository, first, _second = email_context
    address = "status@example.com"
    code = "654321"
    repository.set_candidate_address(first.id, address, changed_at=NOW)
    challenge = repository.create_verification_challenge(
        first.id,
        email_address=address,
        code_hmac=verification_code_hmac(PEPPER, first.id, address, code),
        created_at=NOW,
    )
    if send_outcome is not None:
        repository.finish_verification_send(
            challenge.id,
            first.id,
            EmailSendResult(send_outcome, error_code="bounded-test-code"),
        )

    if confirmation_allowed:
        settings = repository.confirm_verification(
            first.id,
            email_address=address,
            submitted_hmac=verification_code_hmac(
                PEPPER,
                first.id,
                address,
                code,
            ),
            verified_at=NOW + timedelta(seconds=1),
        )
        assert settings.verification_status.value == "verified"
    else:
        with pytest.raises(
            EmailVerificationError,
            match="verification code is invalid or expired",
        ):
            repository.confirm_verification(
                first.id,
                email_address=address,
                submitted_hmac=verification_code_hmac(
                    PEPPER,
                    first.id,
                    address,
                    code,
                ),
                verified_at=NOW + timedelta(seconds=1),
            )


def test_wrong_code_is_limited_to_five_attempts(email_context):
    database, repository, first, _second = email_context
    repository.set_candidate_address(first.id, "verify@example.com", changed_at=NOW)
    create_sent_challenge(repository, first.id, "verify@example.com", "111111")
    wrong = verification_code_hmac(
        PEPPER, first.id, "verify@example.com", "222222"
    )
    for offset in range(5):
        with pytest.raises(EmailVerificationError):
            repository.confirm_verification(
                first.id,
                email_address="verify@example.com",
                submitted_hmac=wrong,
                verified_at=NOW + timedelta(seconds=offset + 1),
            )
    with database.connection() as connection:
        attempts = connection.execute(
            "SELECT attempt_count FROM email_verification_challenges"
        ).fetchone()[0]
    assert attempts == 5
    with pytest.raises(EmailVerificationError):
        repository.confirm_verification(
            first.id,
            email_address="verify@example.com",
            submitted_hmac=verification_code_hmac(
                PEPPER, first.id, "verify@example.com", "111111"
            ),
            verified_at=NOW + timedelta(seconds=10),
        )


def test_expired_and_superseded_codes_fail_generically(email_context):
    database, repository, first, _second = email_context
    address = "verify@example.com"
    repository.set_candidate_address(first.id, address, changed_at=NOW)
    first_challenge = create_sent_challenge(
        repository, first.id, address, "111111", NOW
    )
    second_challenge = create_sent_challenge(
        repository,
        first.id,
        address,
        "222222",
        NOW + timedelta(seconds=61),
    )
    with database.connection() as connection:
        invalidated = connection.execute(
            "SELECT invalidated_at FROM email_verification_challenges WHERE id = ?",
            (first_challenge.id,),
        ).fetchone()[0]
    assert invalidated is not None
    assert second_challenge.id != first_challenge.id
    with pytest.raises(EmailVerificationError):
        repository.confirm_verification(
            first.id,
            email_address=address,
            submitted_hmac=verification_code_hmac(
                PEPPER, first.id, address, "111111"
            ),
            verified_at=NOW + timedelta(seconds=62),
        )
    with pytest.raises(EmailVerificationError):
        repository.confirm_verification(
            first.id,
            email_address=address,
            submitted_hmac=verification_code_hmac(
                PEPPER, first.id, address, "222222"
            ),
            verified_at=NOW + timedelta(minutes=11, seconds=1),
        )


def test_resend_cooldown_and_five_per_hour_limit(email_context):
    _database, repository, first, _second = email_context
    address = "verify@example.com"
    repository.set_candidate_address(first.id, address, changed_at=NOW)
    create_sent_challenge(repository, first.id, address, "000000", NOW)
    with pytest.raises(EmailRateLimitError, match="wait"):
        create_sent_challenge(
            repository, first.id, address, "000001", NOW + timedelta(seconds=59)
        )
    for index in range(1, 5):
        create_sent_challenge(
            repository,
            first.id,
            address,
            f"{index:06d}",
            NOW + timedelta(seconds=61 * index),
        )
    with pytest.raises(EmailRateLimitError, match="hourly"):
        create_sent_challenge(
            repository,
            first.id,
            address,
            "999999",
            NOW + timedelta(seconds=61 * 5),
        )


def test_account_wide_verification_limit_blocks_eleventh_across_addresses(
    email_context,
):
    _database, repository, first, _second = email_context
    first_address = "first-candidate@example.com"
    second_address = "second-candidate@example.com"
    repository.set_candidate_address(first.id, first_address, changed_at=NOW)
    for index in range(5):
        create_sent_challenge(
            repository,
            first.id,
            first_address,
            f"{index:06d}",
            NOW + timedelta(seconds=61 * index),
        )

    second_start = NOW + timedelta(seconds=61 * 5)
    repository.set_candidate_address(
        first.id,
        second_address,
        changed_at=second_start,
    )
    for index in range(5):
        create_sent_challenge(
            repository,
            first.id,
            second_address,
            f"{index + 5:06d}",
            second_start + timedelta(seconds=61 * index),
        )

    third_address = "third-candidate@example.com"
    eleventh_attempt = second_start + timedelta(seconds=61 * 5)
    repository.set_candidate_address(
        first.id,
        third_address,
        changed_at=eleventh_attempt,
    )
    with pytest.raises(EmailRateLimitError, match="account hourly"):
        create_sent_challenge(
            repository,
            first.id,
            third_address,
            "999999",
            eleventh_attempt,
        )


def test_account_wide_verification_limit_is_user_scoped(email_context):
    _database, repository, first, second = email_context
    for index in range(10):
        address = f"first-user-{index}@example.com"
        repository.set_candidate_address(first.id, address, changed_at=NOW)
        create_sent_challenge(repository, first.id, address, f"{index:06d}", NOW)

    second_address = "second-user@example.com"
    repository.set_candidate_address(second.id, second_address, changed_at=NOW)
    challenge = create_sent_challenge(
        repository,
        second.id,
        second_address,
        "123456",
        NOW,
    )
    assert challenge.user_id == second.id


def test_account_wide_verification_limit_excludes_rows_older_than_one_hour(
    email_context,
):
    _database, repository, first, _second = email_context
    for index in range(10):
        address = f"expired-window-{index}@example.com"
        repository.set_candidate_address(first.id, address, changed_at=NOW)
        create_sent_challenge(repository, first.id, address, f"{index:06d}", NOW)

    next_address = "new-window@example.com"
    next_window = NOW + timedelta(hours=1, seconds=1)
    repository.set_candidate_address(first.id, next_address, changed_at=next_window)
    challenge = create_sent_challenge(
        repository,
        first.id,
        next_address,
        "999999",
        next_window,
    )
    assert challenge.email_address == next_address


def test_candidate_address_edits_do_not_consume_verification_send_quota(
    email_context,
):
    _database, repository, first, _second = email_context
    for index in range(20):
        repository.set_candidate_address(
            first.id,
            f"edited-{index}@example.com",
            changed_at=NOW,
        )

    current_address = "edited-19@example.com"
    challenge = create_sent_challenge(
        repository,
        first.id,
        current_address,
        "123456",
        NOW,
    )
    assert challenge.email_address == current_address


def test_provider_paused_same_address_cannot_self_clear(email_context):
    _database, repository, first, _second = email_context
    address = "blocked@example.com"
    repository.set_candidate_address(first.id, address, changed_at=NOW)
    repository.pause_matching_address(
        first.id,
        address,
        EmailPauseReason.BLACKLISTED,
        paused_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(EmailAddressPausedError):
        repository.set_candidate_address(
            first.id,
            " BLOCKED@example.com ",
            changed_at=NOW + timedelta(seconds=2),
        )
    recovered = repository.set_candidate_address(
        first.id,
        "different@example.com",
        changed_at=NOW + timedelta(seconds=3),
    )
    assert recovered.health_status.value == "healthy"
    assert recovered.verification_status.value == "pending"


def test_test_send_rate_limit_does_not_require_reminder(email_context):
    _database, repository, first, _second = email_context
    address = "test@example.com"
    repository.set_candidate_address(first.id, address, changed_at=NOW)
    create_sent_challenge(repository, first.id, address, "123456")
    repository.confirm_verification(
        first.id,
        email_address=address,
        submitted_hmac=verification_code_hmac(
            PEPPER, first.id, address, "123456"
        ),
        verified_at=NOW + timedelta(seconds=1),
    )
    assert repository.reserve_test_send(
        first.id, sent_at=NOW + timedelta(minutes=1)
    ) == address
    with pytest.raises(EmailRateLimitError):
        repository.reserve_test_send(
            first.id, sent_at=NOW + timedelta(minutes=1, seconds=59)
        )
