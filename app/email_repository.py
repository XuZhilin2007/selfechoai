from __future__ import annotations

import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.database import Database
from app.schemas import (
    EmailDeliveryStatus,
    EmailHealthStatus,
    EmailPauseReason,
    EmailProviderDeliveryStatus,
    EmailVerificationStatus,
)
from app.services.tencent_ses import (
    EmailSendOutcome,
    EmailSendResult,
    EmailStatusQueryOutcome,
    EmailStatusResult,
)
from app.time_utils import serialize_utc_datetime


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFICATION_EXPIRY = timedelta(minutes=10)
VERIFICATION_RESEND_COOLDOWN = timedelta(seconds=60)
VERIFICATION_HOURLY_LIMIT = 5
VERIFICATION_ACCOUNT_HOURLY_LIMIT = 10
TEST_SEND_COOLDOWN = timedelta(seconds=60)
TEST_SEND_HOURLY_LIMIT = 5
EMAIL_DELIVERY_TTL = timedelta(minutes=15)
EMAIL_MAX_ATTEMPTS = 4
EMAIL_RETRY_DELAYS = {
    1: timedelta(seconds=60),
    2: timedelta(seconds=180),
    3: timedelta(seconds=360),
}
STATUS_RECHECK_DELAYS = {
    1: timedelta(minutes=5),
    2: timedelta(minutes=60),
}


class EmailRepositoryError(RuntimeError):
    pass


class EmailRateLimitError(EmailRepositoryError):
    pass


class EmailVerificationError(EmailRepositoryError):
    pass


class EmailAddressPausedError(EmailRepositoryError):
    pass


class EmailSettingsUnavailableError(EmailRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class EmailSettingsRecord:
    user_id: int
    email_address: str | None
    verification_status: EmailVerificationStatus
    verified_at: datetime | None
    enabled: bool
    health_status: EmailHealthStatus
    pause_reason: EmailPauseReason | None
    last_test_sent_at: datetime | None
    test_send_window_started_at: datetime | None
    test_send_count: int

    @property
    def effective_active(self) -> bool:
        return bool(
            self.enabled
            and self.email_address
            and self.verification_status == EmailVerificationStatus.VERIFIED
            and self.health_status == EmailHealthStatus.HEALTHY
        )


@dataclass(frozen=True, slots=True)
class VerificationChallenge:
    id: int
    user_id: int
    email_address: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedEmailDelivery:
    id: int
    user_id: int
    reminder_id: int
    destination_email: str
    remind_at: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class EmailStatusCheckTarget:
    id: int
    user_id: int
    destination_email: str
    provider_message_id: str
    provider_request_date: str
    status_check_count: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_reminder_email(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        len(normalized) < 3
        or len(normalized) > 320
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or not EMAIL_PATTERN.fullmatch(normalized)
    ):
        raise ValueError("invalid email address")
    return normalized


class EmailReminderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_settings(self, user_id: int) -> EmailSettingsRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._settings_from_row(row, user_id=user_id)

    def set_candidate_address(
        self,
        user_id: int,
        email_address: str,
        *,
        changed_at: datetime | None = None,
    ) -> EmailSettingsRecord:
        address = normalize_reminder_email(email_address)
        now_value = _serialize(changed_at or utc_now())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO email_reminder_settings (
                        user_id, email_address, verification_status, enabled,
                        health_status, created_time, updated_time
                    ) VALUES (?, ?, 'pending', 0, 'healthy', ?, ?)
                    """,
                    (user_id, address, now_value, now_value),
                )
            elif row["email_address"] == address:
                if row["health_status"] == EmailHealthStatus.PAUSED.value:
                    raise EmailAddressPausedError(
                        "a different email address is required"
                    )
            else:
                connection.execute(
                    """
                    UPDATE email_reminder_settings
                    SET email_address = ?, verification_status = 'pending',
                        verified_at = NULL, health_status = 'healthy',
                        pause_reason = NULL, updated_time = ?
                    WHERE user_id = ?
                    """,
                    (address, now_value, user_id),
                )
                connection.execute(
                    """
                    UPDATE email_verification_challenges
                    SET invalidated_at = ?
                    WHERE user_id = ? AND consumed_at IS NULL
                      AND invalidated_at IS NULL
                    """,
                    (now_value, user_id),
                )
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'suppressed', finished_at = ?,
                        provider_status = 'destination_changed',
                        last_error_class = 'suppressed', updated_time = ?
                    WHERE user_id = ? AND status IN ('queued', 'retry_wait')
                    """,
                    (now_value, now_value, user_id),
                )
            updated = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._settings_from_row(updated, user_id=user_id)

    def set_enabled(
        self,
        user_id: int,
        enabled: bool,
        *,
        changed_at: datetime | None = None,
    ) -> EmailSettingsRecord:
        now_value = _serialize(changed_at or utc_now())
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO email_reminder_settings (
                    user_id, enabled, created_time, updated_time
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_time = excluded.updated_time
                """,
                (user_id, int(enabled), now_value, now_value),
            )
            if not enabled:
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'suppressed', finished_at = ?,
                        provider_status = 'email_disabled',
                        last_error_class = 'suppressed', updated_time = ?
                    WHERE user_id = ? AND status IN ('queued', 'retry_wait')
                    """,
                    (now_value, now_value, user_id),
                )
            row = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._settings_from_row(row, user_id=user_id)

    def create_verification_challenge(
        self,
        user_id: int,
        *,
        email_address: str,
        code_hmac: str,
        created_at: datetime | None = None,
    ) -> VerificationChallenge:
        now = created_at or utc_now()
        now_value = _serialize(now)
        expires_value = _serialize(now + VERIFICATION_EXPIRY)
        one_hour_ago = _serialize(now - timedelta(hours=1))
        cooldown_after = _serialize(now - VERIFICATION_RESEND_COOLDOWN)
        with self.database.transaction() as connection:
            settings = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if settings is None or settings["email_address"] != email_address:
                raise EmailVerificationError("verification request is no longer valid")
            if settings["health_status"] == EmailHealthStatus.PAUSED.value:
                raise EmailAddressPausedError("a different email address is required")
            recent = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(last_sent_at) AS latest
                FROM email_verification_challenges
                WHERE user_id = ? AND email_address = ? AND last_sent_at >= ?
                """,
                (user_id, email_address, one_hour_ago),
            ).fetchone()
            if recent["latest"] is not None and recent["latest"] > cooldown_after:
                raise EmailRateLimitError(
                    "please wait before requesting another verification email"
                )
            if int(recent["count"]) >= VERIFICATION_HOURLY_LIMIT:
                raise EmailRateLimitError("verification email hourly limit reached")
            account_recent = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM email_verification_challenges
                WHERE user_id = ? AND last_sent_at >= ?
                """,
                (user_id, one_hour_ago),
            ).fetchone()
            if int(account_recent["count"]) >= VERIFICATION_ACCOUNT_HOURLY_LIMIT:
                raise EmailRateLimitError(
                    "verification email account hourly limit reached"
                )
            connection.execute(
                """
                UPDATE email_verification_challenges
                SET invalidated_at = ?
                WHERE user_id = ? AND email_address = ?
                  AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (now_value, user_id, email_address),
            )
            cursor = connection.execute(
                """
                INSERT INTO email_verification_challenges (
                    user_id, email_address, code_hmac, send_status,
                    expires_at, created_time, last_sent_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    user_id,
                    email_address,
                    code_hmac,
                    expires_value,
                    now_value,
                    now_value,
                ),
            )
        return VerificationChallenge(
            id=int(cursor.lastrowid),
            user_id=user_id,
            email_address=email_address,
            expires_at=now + VERIFICATION_EXPIRY,
        )

    def finish_verification_send(
        self,
        challenge_id: int,
        user_id: int,
        result: EmailSendResult,
    ) -> None:
        send_status = {
            EmailSendOutcome.ACCEPTED: "accepted",
            EmailSendOutcome.RETRYABLE_FAILURE: "failed",
            EmailSendOutcome.PERMANENT_FAILURE: "failed",
            EmailSendOutcome.AMBIGUOUS_FAILURE: "unknown",
        }[result.outcome]
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE email_verification_challenges
                SET send_status = ?, provider_message_id = ?,
                    provider_request_id = ?, provider_request_date = ?
                WHERE id = ? AND user_id = ? AND send_status = 'pending'
                """,
                (
                    send_status,
                    result.provider_message_id,
                    result.provider_request_id,
                    result.provider_request_date,
                    challenge_id,
                    user_id,
                ),
            )

    def confirm_verification(
        self,
        user_id: int,
        *,
        email_address: str,
        submitted_hmac: str,
        verified_at: datetime | None = None,
    ) -> EmailSettingsRecord:
        now = verified_at or utc_now()
        now_value = _serialize(now)
        verification_failed = False
        updated: sqlite3.Row | None = None
        with self.database.transaction() as connection:
            settings = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if (
                settings is None
                or settings["email_address"] != email_address
                or settings["health_status"] == EmailHealthStatus.PAUSED.value
            ):
                raise EmailVerificationError("verification code is invalid or expired")
            challenge = connection.execute(
                """
                SELECT * FROM email_verification_challenges
                WHERE user_id = ? AND email_address = ?
                  AND send_status IN ('accepted', 'unknown')
                  AND consumed_at IS NULL AND invalidated_at IS NULL
                  AND expires_at > ? AND attempt_count < 5
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, email_address, now_value),
            ).fetchone()
            if challenge is None:
                raise EmailVerificationError("verification code is invalid or expired")
            if not hmac.compare_digest(challenge["code_hmac"], submitted_hmac):
                connection.execute(
                    """
                    UPDATE email_verification_challenges
                    SET attempt_count = attempt_count + 1
                    WHERE id = ? AND attempt_count < 5
                    """,
                    (challenge["id"],),
                )
                verification_failed = True
            else:
                connection.execute(
                    """
                    UPDATE email_verification_challenges
                    SET consumed_at = ? WHERE id = ?
                    """,
                    (now_value, challenge["id"]),
                )
                cursor = connection.execute(
                    """
                    UPDATE email_reminder_settings
                    SET verification_status = 'verified', verified_at = ?,
                        updated_time = ?
                    WHERE user_id = ? AND email_address = ?
                      AND health_status = 'healthy'
                    """,
                    (now_value, now_value, user_id, email_address),
                )
                if cursor.rowcount != 1:
                    raise EmailVerificationError(
                        "verification code is invalid or expired"
                    )
                updated = connection.execute(
                    "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        if verification_failed or updated is None:
            raise EmailVerificationError("verification code is invalid or expired")
        return self._settings_from_row(updated, user_id=user_id)

    def reserve_test_send(
        self,
        user_id: int,
        *,
        sent_at: datetime | None = None,
    ) -> str:
        now = sent_at or utc_now()
        now_value = _serialize(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM email_reminder_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if (
                row is None
                or row["email_address"] is None
                or row["verification_status"]
                != EmailVerificationStatus.VERIFIED.value
                or row["health_status"] != EmailHealthStatus.HEALTHY.value
            ):
                raise EmailSettingsUnavailableError(
                    "a verified healthy enabled email address is required"
                )
            last_sent = _optional_datetime(row["last_test_sent_at"])
            if last_sent is not None and now - last_sent < TEST_SEND_COOLDOWN:
                raise EmailRateLimitError("please wait before sending another test email")
            window_start = _optional_datetime(row["test_send_window_started_at"])
            count = int(row["test_send_count"])
            if window_start is None or now - window_start >= timedelta(hours=1):
                window_start = now
                count = 0
            if count >= TEST_SEND_HOURLY_LIMIT:
                raise EmailRateLimitError("test email hourly limit reached")
            connection.execute(
                """
                UPDATE email_reminder_settings
                SET last_test_sent_at = ?, test_send_window_started_at = ?,
                    test_send_count = ?, updated_time = ?
                WHERE user_id = ?
                """,
                (now_value, _serialize(window_start), count + 1, now_value, user_id),
            )
        return str(row["email_address"])

    def pause_matching_address(
        self,
        user_id: int,
        email_address: str,
        reason: EmailPauseReason,
        *,
        paused_at: datetime | None = None,
    ) -> bool:
        now_value = _serialize(paused_at or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE email_reminder_settings
                SET health_status = 'paused', pause_reason = ?, updated_time = ?
                WHERE user_id = ? AND email_address = ?
                """,
                (reason.value, now_value, user_id, email_address),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'suppressed', finished_at = ?,
                        provider_status = 'address_paused',
                        last_error_class = 'suppressed', updated_time = ?
                    WHERE user_id = ? AND destination_email = ?
                      AND status IN ('queued', 'retry_wait')
                    """,
                    (now_value, now_value, user_id, email_address),
                )
        return cursor.rowcount == 1

    def get_delivery_row(self, delivery_id: int) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM reminder_email_deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()

    def reconcile_stale_sending(
        self,
        *,
        as_of: datetime | None = None,
        stale_after_seconds: int,
    ) -> int:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        now = as_of or utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE reminder_email_deliveries
                SET status = 'unknown', finished_at = ?,
                    provider_status = 'stale_sending',
                    last_error_class = 'ambiguous', updated_time = ?
                WHERE status = 'sending'
                  AND (last_attempted_at IS NULL OR last_attempted_at <= ?)
                """,
                (
                    _serialize(now),
                    _serialize(now),
                    _serialize(now - timedelta(seconds=stale_after_seconds)),
                ),
            )
        return cursor.rowcount

    def claim_next_delivery(
        self,
        *,
        as_of: datetime | None = None,
    ) -> ClaimedEmailDelivery | None:
        now = as_of or utc_now()
        now_value = _serialize(now)
        while True:
            with self.database.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT deliveries.*, reminders.remind_at,
                           reminders.status AS reminder_status,
                           items.status AS item_status,
                           settings.email_address AS current_email,
                           settings.verification_status,
                           settings.enabled, settings.health_status,
                           users.status AS user_status
                    FROM reminder_email_deliveries AS deliveries
                    JOIN reminders
                      ON reminders.id = deliveries.reminder_id
                     AND reminders.user_id = deliveries.user_id
                    LEFT JOIN personal_items AS items
                      ON items.id = reminders.item_id
                     AND items.user_id = reminders.user_id
                    LEFT JOIN email_reminder_settings AS settings
                      ON settings.user_id = deliveries.user_id
                    LEFT JOIN users ON users.id = deliveries.user_id
                    WHERE deliveries.status = 'queued'
                       OR (deliveries.status = 'retry_wait'
                           AND deliveries.next_attempt_at <= ?)
                    ORDER BY COALESCE(deliveries.next_attempt_at,
                                      deliveries.created_time), deliveries.id
                    LIMIT 1
                    """,
                    (now_value,),
                ).fetchone()
                if row is None:
                    return None
                terminal = self._pre_send_terminal_status(row, now)
                if terminal is not None:
                    status, reason = terminal
                    connection.execute(
                        """
                        UPDATE reminder_email_deliveries
                        SET status = ?, finished_at = ?, provider_status = ?,
                            last_error_class = ?, next_attempt_at = NULL,
                            updated_time = ?
                        WHERE id = ? AND status IN ('queued', 'retry_wait')
                        """,
                        (
                            status.value,
                            now_value,
                            reason,
                            "expired" if status == EmailDeliveryStatus.EXPIRED else "suppressed",
                            now_value,
                            row["id"],
                        ),
                    )
                    continue
                cursor = connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'sending', attempt_count = attempt_count + 1,
                        first_attempted_at = COALESCE(first_attempted_at, ?),
                        last_attempted_at = ?, next_attempt_at = NULL,
                        finished_at = NULL, provider_status = NULL,
                        last_error_class = NULL, updated_time = ?
                    WHERE id = ? AND status IN ('queued', 'retry_wait')
                      AND attempt_count < ?
                    """,
                    (
                        now_value,
                        now_value,
                        now_value,
                        row["id"],
                        EMAIL_MAX_ATTEMPTS,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                return ClaimedEmailDelivery(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    reminder_id=int(row["reminder_id"]),
                    destination_email=str(row["destination_email"]),
                    remind_at=datetime.fromisoformat(row["remind_at"]),
                    attempt_count=int(row["attempt_count"]) + 1,
                )

    def revalidate_claimed_delivery(
        self,
        delivery_id: int,
        user_id: int,
        *,
        as_of: datetime | None = None,
    ) -> bool:
        now = as_of or utc_now()
        now_value = _serialize(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT deliveries.*, reminders.remind_at,
                       reminders.status AS reminder_status,
                       items.status AS item_status,
                       settings.email_address AS current_email,
                       settings.verification_status,
                       settings.enabled, settings.health_status,
                       users.status AS user_status
                FROM reminder_email_deliveries AS deliveries
                JOIN reminders
                  ON reminders.id = deliveries.reminder_id
                 AND reminders.user_id = deliveries.user_id
                LEFT JOIN personal_items AS items
                  ON items.id = reminders.item_id
                 AND items.user_id = reminders.user_id
                LEFT JOIN email_reminder_settings AS settings
                  ON settings.user_id = deliveries.user_id
                LEFT JOIN users ON users.id = deliveries.user_id
                WHERE deliveries.id = ? AND deliveries.user_id = ?
                  AND deliveries.status = 'sending'
                """,
                (delivery_id, user_id),
            ).fetchone()
            if row is None:
                return False
            terminal = self._pre_send_terminal_status(row, now)
            if terminal is None:
                return True
            status, reason = terminal
            connection.execute(
                """
                UPDATE reminder_email_deliveries
                SET status = ?, finished_at = ?, provider_status = ?,
                    last_error_class = ?, updated_time = ?
                WHERE id = ? AND user_id = ? AND status = 'sending'
                """,
                (
                    status.value,
                    now_value,
                    reason,
                    "expired" if status == EmailDeliveryStatus.EXPIRED else "suppressed",
                    now_value,
                    delivery_id,
                    user_id,
                ),
            )
            return False

    def finish_delivery_attempt(
        self,
        delivery: ClaimedEmailDelivery,
        result: EmailSendResult,
        *,
        finished_at: datetime | None = None,
    ) -> EmailDeliveryStatus:
        now = finished_at or utc_now()
        now_value = _serialize(now)
        with self.database.transaction() as connection:
            current = connection.execute(
                """
                SELECT deliveries.*, reminders.remind_at
                FROM reminder_email_deliveries AS deliveries
                JOIN reminders ON reminders.id = deliveries.reminder_id
                WHERE deliveries.id = ? AND deliveries.user_id = ?
                """,
                (delivery.id, delivery.user_id),
            ).fetchone()
            if current is None or current["status"] != EmailDeliveryStatus.SENDING.value:
                raise EmailRepositoryError("email delivery is no longer sending")
            if result.outcome == EmailSendOutcome.ACCEPTED:
                status = EmailDeliveryStatus.ACCEPTED
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'accepted', accepted_at = ?,
                        provider_message_id = ?, provider_request_id = ?,
                        provider_request_date = ?, provider_status = 'accepted',
                        provider_delivery_status = 'pending',
                        next_status_check_at = ?, updated_time = ?
                    WHERE id = ? AND user_id = ? AND status = 'sending'
                    """,
                    (
                        now_value,
                        result.provider_message_id,
                        result.provider_request_id,
                        result.provider_request_date,
                        _serialize(now + timedelta(seconds=30)),
                        now_value,
                        delivery.id,
                        delivery.user_id,
                    ),
                )
            elif result.outcome == EmailSendOutcome.RETRYABLE_FAILURE:
                delay = EMAIL_RETRY_DELAYS.get(int(current["attempt_count"]))
                expiry = datetime.fromisoformat(current["remind_at"]) + EMAIL_DELIVERY_TTL
                if delay is None:
                    status = EmailDeliveryStatus.FAILED
                elif now + delay >= expiry:
                    status = EmailDeliveryStatus.EXPIRED
                else:
                    status = EmailDeliveryStatus.RETRY_WAIT
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = ?, next_attempt_at = ?, finished_at = ?,
                        provider_request_id = ?, provider_request_date = ?,
                        provider_status = ?, last_error_class = 'retryable',
                        updated_time = ?
                    WHERE id = ? AND user_id = ? AND status = 'sending'
                    """,
                    (
                        status.value,
                        _serialize(now + delay)
                        if status == EmailDeliveryStatus.RETRY_WAIT and delay
                        else None,
                        now_value if status != EmailDeliveryStatus.RETRY_WAIT else None,
                        result.provider_request_id,
                        result.provider_request_date,
                        result.error_code,
                        now_value,
                        delivery.id,
                        delivery.user_id,
                    ),
                )
            elif result.outcome == EmailSendOutcome.PERMANENT_FAILURE:
                status = EmailDeliveryStatus.FAILED
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'failed', finished_at = ?,
                        provider_request_id = ?, provider_request_date = ?,
                        provider_status = ?, last_error_class = 'permanent',
                        updated_time = ?
                    WHERE id = ? AND user_id = ? AND status = 'sending'
                    """,
                    (
                        now_value,
                        result.provider_request_id,
                        result.provider_request_date,
                        result.error_code,
                        now_value,
                        delivery.id,
                        delivery.user_id,
                    ),
                )
            else:
                status = EmailDeliveryStatus.UNKNOWN
                connection.execute(
                    """
                    UPDATE reminder_email_deliveries
                    SET status = 'unknown', finished_at = ?,
                        provider_message_id = ?, provider_request_id = ?,
                        provider_request_date = ?, provider_status = ?,
                        last_error_class = 'ambiguous', updated_time = ?
                    WHERE id = ? AND user_id = ? AND status = 'sending'
                    """,
                    (
                        now_value,
                        result.provider_message_id,
                        result.provider_request_id,
                        result.provider_request_date,
                        result.error_code,
                        now_value,
                        delivery.id,
                        delivery.user_id,
                    ),
                )
            if result.pause_destination:
                self._pause_matching_address_in_connection(
                    connection,
                    delivery.user_id,
                    delivery.destination_email,
                    EmailPauseReason.PROVIDER_SUPPRESSED,
                    now_value,
                )
        return status

    def claim_next_status_check(
        self,
        *,
        as_of: datetime | None = None,
    ) -> EmailStatusCheckTarget | None:
        now_value = _serialize(as_of or utc_now())
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM reminder_email_deliveries
                WHERE status = 'accepted'
                  AND next_status_check_at IS NOT NULL
                  AND next_status_check_at <= ?
                  AND status_check_count < 3
                  AND provider_message_id IS NOT NULL
                  AND provider_request_date IS NOT NULL
                ORDER BY next_status_check_at, id LIMIT 1
                """,
                (now_value,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE reminder_email_deliveries
                SET status_check_count = status_check_count + 1,
                    next_status_check_at = NULL, updated_time = ?
                WHERE id = ? AND status = 'accepted'
                  AND next_status_check_at IS NOT NULL
                """,
                (now_value, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
        return EmailStatusCheckTarget(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            destination_email=str(row["destination_email"]),
            provider_message_id=str(row["provider_message_id"]),
            provider_request_date=str(row["provider_request_date"]),
            status_check_count=int(row["status_check_count"]) + 1,
        )

    def finish_status_check(
        self,
        target: EmailStatusCheckTarget,
        result: EmailStatusResult,
        *,
        checked_at: datetime | None = None,
    ) -> None:
        now = checked_at or utc_now()
        now_value = _serialize(now)
        next_check: datetime | None = None
        provider_delivery_status: EmailProviderDeliveryStatus | None = None
        if result.outcome != EmailStatusQueryOutcome.FOUND:
            delay = STATUS_RECHECK_DELAYS.get(target.status_check_count)
            next_check = now + delay if delay is not None else None
        elif result.send_status != 0:
            provider_delivery_status = EmailProviderDeliveryStatus.REJECTED
        else:
            provider_delivery_status = {
                0: EmailProviderDeliveryStatus.PENDING,
                1: EmailProviderDeliveryStatus.DELIVERED,
                2: EmailProviderDeliveryStatus.DROPPED,
                3: EmailProviderDeliveryStatus.REJECTED,
                8: EmailProviderDeliveryStatus.DEFERRED,
            }.get(result.deliver_status, EmailProviderDeliveryStatus.PENDING)
            if provider_delivery_status in {
                EmailProviderDeliveryStatus.PENDING,
                EmailProviderDeliveryStatus.DEFERRED,
            }:
                delay = STATUS_RECHECK_DELAYS.get(target.status_check_count)
                next_check = now + delay if delay is not None else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE reminder_email_deliveries
                SET provider_delivery_status = COALESCE(?, provider_delivery_status),
                    provider_deliver_message = COALESCE(?, provider_deliver_message),
                    provider_deliver_time = COALESCE(?, provider_deliver_time),
                    provider_status = ?, next_status_check_at = ?, updated_time = ?
                WHERE id = ? AND user_id = ? AND status = 'accepted'
                  AND status_check_count = ?
                """,
                (
                    provider_delivery_status.value
                    if provider_delivery_status is not None
                    else None,
                    result.deliver_message,
                    _serialize(result.deliver_time)
                    if result.deliver_time is not None
                    else None,
                    (
                        f"send_{result.send_status}_deliver_{result.deliver_status}"
                        if result.outcome == EmailStatusQueryOutcome.FOUND
                        else result.error_code or result.outcome.value
                    ),
                    _serialize(next_check) if next_check is not None else None,
                    now_value,
                    target.id,
                    target.user_id,
                    target.status_check_count,
                ),
            )
            if result.pause_destination:
                reason = (
                    EmailPauseReason.COMPLAINT
                    if result.complained
                    else EmailPauseReason.BLACKLISTED
                )
                self._pause_matching_address_in_connection(
                    connection,
                    target.user_id,
                    target.destination_email,
                    reason,
                    now_value,
                )

    @staticmethod
    def _settings_from_row(
        row: sqlite3.Row | None,
        *,
        user_id: int,
    ) -> EmailSettingsRecord:
        if row is None:
            return EmailSettingsRecord(
                user_id=user_id,
                email_address=None,
                verification_status=EmailVerificationStatus.PENDING,
                verified_at=None,
                enabled=False,
                health_status=EmailHealthStatus.HEALTHY,
                pause_reason=None,
                last_test_sent_at=None,
                test_send_window_started_at=None,
                test_send_count=0,
            )
        return EmailSettingsRecord(
            user_id=int(row["user_id"]),
            email_address=row["email_address"],
            verification_status=EmailVerificationStatus(row["verification_status"]),
            verified_at=_optional_datetime(row["verified_at"]),
            enabled=bool(row["enabled"]),
            health_status=EmailHealthStatus(row["health_status"]),
            pause_reason=(
                EmailPauseReason(row["pause_reason"])
                if row["pause_reason"] is not None
                else None
            ),
            last_test_sent_at=_optional_datetime(row["last_test_sent_at"]),
            test_send_window_started_at=_optional_datetime(
                row["test_send_window_started_at"]
            ),
            test_send_count=int(row["test_send_count"]),
        )

    @staticmethod
    def _pre_send_terminal_status(
        row: sqlite3.Row,
        now: datetime,
    ) -> tuple[EmailDeliveryStatus, str] | None:
        remind_at = datetime.fromisoformat(row["remind_at"])
        if now >= remind_at + EMAIL_DELIVERY_TTL:
            return EmailDeliveryStatus.EXPIRED, "ttl_expired"
        if row["reminder_status"] != "due":
            return EmailDeliveryStatus.SUPPRESSED, "reminder_inactive"
        if row["item_status"] != "active":
            return EmailDeliveryStatus.SUPPRESSED, "item_inactive"
        if row["user_status"] != "active":
            return EmailDeliveryStatus.SUPPRESSED, "user_inactive"
        if (
            row["current_email"] != row["destination_email"]
            or row["verification_status"] != EmailVerificationStatus.VERIFIED.value
            or not bool(row["enabled"])
            or row["health_status"] != EmailHealthStatus.HEALTHY.value
        ):
            return EmailDeliveryStatus.SUPPRESSED, "destination_inactive"
        return None

    @staticmethod
    def _pause_matching_address_in_connection(
        connection: sqlite3.Connection,
        user_id: int,
        email_address: str,
        reason: EmailPauseReason,
        now_value: str,
    ) -> None:
        connection.execute(
            """
            UPDATE email_reminder_settings
            SET health_status = 'paused', pause_reason = ?, updated_time = ?
            WHERE user_id = ? AND email_address = ?
            """,
            (reason.value, now_value, user_id, email_address),
        )
        connection.execute(
            """
            UPDATE reminder_email_deliveries
            SET status = 'suppressed', finished_at = ?,
                provider_status = 'address_paused',
                last_error_class = 'suppressed', updated_time = ?
            WHERE user_id = ? AND destination_email = ?
              AND status IN ('queued', 'retry_wait')
            """,
            (now_value, now_value, user_id, email_address),
        )


def _serialize(value: datetime) -> str:
    return serialize_utc_datetime(value, field_name="datetime")


def _optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
