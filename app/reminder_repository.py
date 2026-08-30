from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.database import Database
from app.repository import InvalidOperationError, NotFoundError
from app.schemas import (
    PushSubscriptionRecord,
    PushSubscriptionStatus,
    ReminderCancelReason,
    ReminderCreationCandidate,
    ReminderRecord,
    ReminderStatus,
)
from app.time_utils import serialize_utc_datetime, validate_timezone_name


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class ReminderRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _reminder_from_row(row: sqlite3.Row) -> ReminderRecord:
        return ReminderRecord(
            id=row["id"],
            user_id=row["user_id"],
            item_id=row["item_id"],
            source_expression=row["source_expression"],
            scheduled_timezone=row["scheduled_timezone"],
            remind_at=_optional_datetime(row["remind_at"]),
            status=row["status"],
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
            due_time=_optional_datetime(row["due_time"]),
            cancelled_time=_optional_datetime(row["cancelled_time"]),
            cancel_reason=row["cancel_reason"],
            surfaced_time=_optional_datetime(row["surfaced_time"]),
        )

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> PushSubscriptionRecord:
        return PushSubscriptionRecord(
            id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            endpoint=row["endpoint"],
            p256dh=row["p256dh"],
            auth=row["auth"],
            status=row["status"],
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
            invalidated_time=_optional_datetime(row["invalidated_time"]),
            last_error_code=row["last_error_code"],
        )

    def create_ai_reminder_if_absent(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: int,
        user_id: int,
        reminder: ReminderCreationCandidate,
        created_time: str,
    ) -> None:
        """Create one AI-derived reminder while preserving any active choice."""

        item = connection.execute(
            "SELECT status FROM personal_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if item is None:
            raise NotFoundError("item not found")
        if item["status"] != "active":
            return
        active = connection.execute(
            """
            SELECT 1 FROM reminders
            WHERE item_id = ? AND user_id = ?
              AND status IN ('needs_confirmation', 'scheduled')
            LIMIT 1
            """,
            (item_id, user_id),
        ).fetchone()
        if active is not None:
            return

        remind_at = (
            serialize_utc_datetime(reminder.remind_at, field_name="remind_at")
            if reminder.remind_at is not None
            else None
        )
        status = (
            ReminderStatus.SCHEDULED.value
            if remind_at is not None
            else ReminderStatus.NEEDS_CONFIRMATION.value
        )
        connection.execute(
            """
            INSERT INTO reminders (
                user_id, item_id, source_expression, scheduled_timezone,
                remind_at, status, created_time, updated_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                item_id,
                reminder.source_expression,
                reminder.scheduled_timezone,
                remind_at,
                status,
                created_time,
                created_time,
            ),
        )

    def create_reminder(
        self,
        *,
        item_id: int,
        user_id: int,
        scheduled_timezone: str,
        remind_at: datetime | None = None,
        source_expression: str | None = None,
    ) -> ReminderRecord:
        timezone_name = validate_timezone_name(scheduled_timezone)
        now = utc_now()
        now_value = serialize_utc_datetime(now, field_name="created_time")
        remind_at_value: str | None = None
        status = ReminderStatus.NEEDS_CONFIRMATION
        if remind_at is not None:
            remind_at_value = serialize_utc_datetime(
                remind_at,
                field_name="remind_at",
            )
            if datetime.fromisoformat(remind_at_value) <= now:
                raise ValueError("remind_at must be in the future")
            status = ReminderStatus.SCHEDULED

        expression = source_expression.strip() if source_expression else None
        try:
            with self.database.transaction() as connection:
                item = connection.execute(
                    """
                    SELECT status FROM personal_items
                    WHERE id = ? AND user_id = ?
                    """,
                    (item_id, user_id),
                ).fetchone()
                if item is None:
                    raise NotFoundError("item not found")
                if item["status"] != "active":
                    raise InvalidOperationError(
                        "reminders can only be created for active items"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO reminders (
                        user_id, item_id, source_expression, scheduled_timezone,
                        remind_at, status, created_time, updated_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        item_id,
                        expression,
                        timezone_name,
                        remind_at_value,
                        status.value,
                        now_value,
                        now_value,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                    (cursor.lastrowid, user_id),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            if "uq_reminders_user_item_active" in str(exc) or (
                "reminders.user_id" in str(exc)
                and "reminders.item_id" in str(exc)
            ):
                raise InvalidOperationError(
                    "item already has an active reminder"
                ) from exc
            raise
        return self._reminder_from_row(row)

    def get_reminder(self, reminder_id: int, user_id: int) -> ReminderRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("reminder not found")
        return self._reminder_from_row(row)

    def get_current_reminder_for_item(
        self,
        item_id: int,
        user_id: int,
    ) -> ReminderRecord | None:
        with self.database.connection() as connection:
            item = connection.execute(
                "SELECT 1 FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("item not found")
            row = connection.execute(
                """
                SELECT * FROM reminders
                WHERE item_id = ? AND user_id = ?
                  AND status IN ('needs_confirmation', 'scheduled')
                ORDER BY created_time DESC, id DESC
                LIMIT 1
                """,
                (item_id, user_id),
            ).fetchone()
        return self._reminder_from_row(row) if row is not None else None

    def get_relevant_reminder_for_item(
        self,
        item_id: int,
        user_id: int,
    ) -> ReminderRecord | None:
        """Return the active Reminder, or the newest historical Reminder."""

        with self.database.connection() as connection:
            item = connection.execute(
                "SELECT 1 FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("item not found")
            row = connection.execute(
                """
                SELECT * FROM reminders
                WHERE item_id = ? AND user_id = ?
                ORDER BY
                    CASE WHEN status IN ('needs_confirmation', 'scheduled')
                         THEN 0 ELSE 1 END,
                    created_time DESC,
                    id DESC
                LIMIT 1
                """,
                (item_id, user_id),
            ).fetchone()
        return self._reminder_from_row(row) if row is not None else None

    def should_offer_reminder_prompt(self, item_id: int, user_id: int) -> bool:
        with self.database.connection() as connection:
            item = connection.execute(
                """
                SELECT status, reminder_prompt_dismissed_at
                FROM personal_items
                WHERE id = ? AND user_id = ?
                """,
                (item_id, user_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("item not found")
            has_reminder = connection.execute(
                """
                SELECT 1 FROM reminders
                WHERE item_id = ? AND user_id = ?
                LIMIT 1
                """,
                (item_id, user_id),
            ).fetchone()
        return (
            item["status"] == "active"
            and item["reminder_prompt_dismissed_at"] is None
            and has_reminder is None
        )

    def mark_due_reminders(
        self,
        user_id: int,
        *,
        as_of: datetime | None = None,
    ) -> int:
        """Lazily transition this user's due reminders without delivery writes."""

        due_value = serialize_utc_datetime(as_of or utc_now(), field_name="as_of")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET status = 'due', due_time = ?, updated_time = ?
                WHERE user_id = ?
                  AND status = 'scheduled'
                  AND remind_at <= ?
                  AND EXISTS (
                      SELECT 1 FROM personal_items AS items
                      WHERE items.id = reminders.item_id
                        AND items.user_id = reminders.user_id
                        AND items.status = 'active'
                  )
                """,
                (due_value, due_value, user_id, due_value),
            )
        return cursor.rowcount

    def list_upcoming(
        self,
        user_id: int,
        *,
        as_of: datetime | None = None,
    ) -> list[ReminderRecord]:
        cutoff = serialize_utc_datetime(as_of or utc_now(), field_name="as_of")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'scheduled' AND remind_at > ?
                ORDER BY remind_at ASC, id ASC
                """,
                (user_id, cutoff),
            ).fetchall()
        return [self._reminder_from_row(row) for row in rows]

    def list_due(
        self,
        user_id: int,
        *,
        unsurfaced_only: bool = False,
    ) -> list[ReminderRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'due'
                  AND (? = 0 OR surfaced_time IS NULL)
                ORDER BY due_time ASC, id ASC
                """,
                (user_id, int(unsurfaced_only)),
            ).fetchall()
        return [self._reminder_from_row(row) for row in rows]

    def reschedule(
        self,
        reminder_id: int,
        user_id: int,
        *,
        remind_at: datetime,
        scheduled_timezone: str,
    ) -> ReminderRecord:
        timezone_name = validate_timezone_name(scheduled_timezone)
        now = utc_now()
        remind_at_value = serialize_utc_datetime(remind_at, field_name="remind_at")
        if datetime.fromisoformat(remind_at_value) <= now:
            raise ValueError("remind_at must be in the future")
        now_value = serialize_utc_datetime(now, field_name="updated_time")

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("reminder not found")
            if row["status"] not in {
                ReminderStatus.NEEDS_CONFIRMATION.value,
                ReminderStatus.SCHEDULED.value,
            }:
                raise InvalidOperationError(
                    "only an active reminder can be rescheduled"
                )
            connection.execute(
                """
                UPDATE reminders
                SET remind_at = ?, scheduled_timezone = ?, status = 'scheduled',
                    updated_time = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    remind_at_value,
                    timezone_name,
                    now_value,
                    reminder_id,
                    user_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
        return self._reminder_from_row(updated)

    def cancel(
        self,
        reminder_id: int,
        user_id: int,
        *,
        reason: ReminderCancelReason = ReminderCancelReason.USER_CANCELLED,
    ) -> ReminderRecord:
        now_value = serialize_utc_datetime(utc_now(), field_name="cancelled_time")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("reminder not found")
            if row["status"] not in {
                ReminderStatus.NEEDS_CONFIRMATION.value,
                ReminderStatus.SCHEDULED.value,
            }:
                raise InvalidOperationError(
                    "only an active reminder can be cancelled"
                )
            connection.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', cancelled_time = ?, cancel_reason = ?,
                    updated_time = ?
                WHERE id = ? AND user_id = ?
                """,
                (now_value, reason.value, now_value, reminder_id, user_id),
            )
            updated = connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
        return self._reminder_from_row(updated)

    def mark_due(
        self,
        reminder_id: int,
        user_id: int,
        *,
        due_time: datetime,
    ) -> ReminderRecord:
        due_value = serialize_utc_datetime(due_time, field_name="due_time")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT status, remind_at FROM reminders
                WHERE id = ? AND user_id = ?
                """,
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("reminder not found")
            if row["status"] != ReminderStatus.SCHEDULED.value:
                raise InvalidOperationError(
                    "only a scheduled reminder can become due"
                )
            if row["remind_at"] > due_value:
                raise InvalidOperationError("reminder time has not arrived")
            connection.execute(
                """
                UPDATE reminders
                SET status = 'due', due_time = ?, updated_time = ?
                WHERE id = ? AND user_id = ? AND status = 'scheduled'
                """,
                (due_value, due_value, reminder_id, user_id),
            )
            updated = connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
        return self._reminder_from_row(updated)

    def mark_surfaced(
        self,
        reminder_id: int,
        user_id: int,
        *,
        surfaced_time: datetime,
    ) -> ReminderRecord:
        surfaced_value = serialize_utc_datetime(
            surfaced_time,
            field_name="surfaced_time",
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("reminder not found")
            if row["status"] != ReminderStatus.DUE.value:
                raise InvalidOperationError("only a due reminder can be surfaced")
            connection.execute(
                """
                UPDATE reminders
                SET surfaced_time = COALESCE(surfaced_time, ?), updated_time = ?
                WHERE id = ? AND user_id = ?
                """,
                (surfaced_value, surfaced_value, reminder_id, user_id),
            )
            updated = connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
        return self._reminder_from_row(updated)

    def dismiss_reminder_prompt(
        self,
        item_id: int,
        user_id: int,
        *,
        dismissed_time: datetime | None = None,
    ) -> datetime:
        dismissed = datetime.fromisoformat(
            serialize_utc_datetime(
                dismissed_time or utc_now(),
                field_name="reminder_prompt_dismissed_at",
            )
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE personal_items
                SET reminder_prompt_dismissed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (dismissed.isoformat(timespec="seconds"), item_id, user_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("item not found")
        return dismissed

    def sync_push_subscription(
        self,
        *,
        user_id: int,
        session_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> PushSubscriptionRecord:
        """Idempotently bind one endpoint to the current user and session."""

        endpoint_value = endpoint.strip()
        p256dh_value = p256dh.strip()
        auth_value = auth.strip()
        if not endpoint_value or not p256dh_value or not auth_value:
            raise ValueError("invalid push subscription")
        now_value = serialize_utc_datetime(utc_now(), field_name="updated_time")

        with self.database.transaction() as connection:
            session = connection.execute(
                """
                SELECT 1 FROM user_sessions
                WHERE id = ? AND user_id = ? AND revoked_time IS NULL
                  AND expires_time > ?
                """,
                (session_id, user_id, now_value),
            ).fetchone()
            if session is None:
                raise NotFoundError("session not found")
            connection.execute(
                """
                UPDATE push_subscriptions
                SET status = 'revoked', invalidated_time = ?, updated_time = ?,
                    last_error_code = NULL
                WHERE user_id = ? AND session_id = ? AND status = 'active'
                  AND endpoint != ?
                """,
                (now_value, now_value, user_id, session_id, endpoint_value),
            )
            existing = connection.execute(
                "SELECT id, user_id FROM push_subscriptions WHERE endpoint = ?",
                (endpoint_value,),
            ).fetchone()
            if existing is not None and existing["user_id"] != user_id:
                raise InvalidOperationError("push subscription is unavailable")
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO push_subscriptions (
                        user_id, session_id, endpoint, p256dh, auth, status,
                        created_time, updated_time
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        user_id,
                        session_id,
                        endpoint_value,
                        p256dh_value,
                        auth_value,
                        now_value,
                        now_value,
                    ),
                )
                subscription_id = cursor.lastrowid
            else:
                subscription_id = existing["id"]
                connection.execute(
                    """
                    UPDATE push_subscriptions
                    SET session_id = ?, p256dh = ?, auth = ?, status = 'active',
                        updated_time = ?, invalidated_time = NULL,
                        last_error_code = NULL
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        session_id,
                        p256dh_value,
                        auth_value,
                        now_value,
                        subscription_id,
                        user_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id = ? AND user_id = ?",
                (subscription_id, user_id),
            ).fetchone()
        return self._subscription_from_row(row)

    def list_active_push_subscriptions(
        self,
        user_id: int,
        *,
        as_of: datetime | None = None,
    ) -> list[PushSubscriptionRecord]:
        cutoff = serialize_utc_datetime(as_of or utc_now(), field_name="as_of")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT subscriptions.*
                FROM push_subscriptions AS subscriptions
                JOIN user_sessions AS sessions
                  ON sessions.id = subscriptions.session_id
                 AND sessions.user_id = subscriptions.user_id
                WHERE subscriptions.user_id = ?
                  AND subscriptions.status = 'active'
                  AND sessions.revoked_time IS NULL
                  AND sessions.expires_time > ?
                ORDER BY subscriptions.id
                """,
                (user_id, cutoff),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def get_active_push_subscription_for_session(
        self,
        user_id: int,
        session_id: int,
        *,
        as_of: datetime | None = None,
    ) -> PushSubscriptionRecord:
        cutoff = serialize_utc_datetime(as_of or utc_now(), field_name="as_of")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT subscriptions.*
                FROM push_subscriptions AS subscriptions
                JOIN user_sessions AS sessions
                  ON sessions.id = subscriptions.session_id
                 AND sessions.user_id = subscriptions.user_id
                WHERE subscriptions.user_id = ?
                  AND subscriptions.session_id = ?
                  AND subscriptions.status = 'active'
                  AND sessions.revoked_time IS NULL
                  AND sessions.expires_time > ?
                ORDER BY subscriptions.updated_time DESC, subscriptions.id DESC
                LIMIT 1
                """,
                (user_id, session_id, cutoff),
            ).fetchone()
        if row is None:
            raise NotFoundError("active push subscription not found")
        return self._subscription_from_row(row)

    def revoke_push_subscription(
        self,
        subscription_id: int,
        user_id: int,
        session_id: int,
    ) -> PushSubscriptionRecord:
        now_value = serialize_utc_datetime(utc_now(), field_name="invalidated_time")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE push_subscriptions
                SET status = 'revoked', invalidated_time = ?, updated_time = ?
                WHERE id = ? AND user_id = ? AND session_id = ?
                  AND status = 'active'
                """,
                (now_value, now_value, subscription_id, user_id, session_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("push subscription not found")
            row = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id = ? AND user_id = ?",
                (subscription_id, user_id),
            ).fetchone()
        return self._subscription_from_row(row)

    def revoke_current_session_push_subscription(
        self,
        user_id: int,
        session_id: int,
    ) -> PushSubscriptionRecord:
        now_value = serialize_utc_datetime(utc_now(), field_name="invalidated_time")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM push_subscriptions
                WHERE user_id = ? AND session_id = ? AND status = 'active'
                ORDER BY updated_time DESC, id DESC
                LIMIT 1
                """,
                (user_id, session_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("active push subscription not found")
            connection.execute(
                """
                UPDATE push_subscriptions
                SET status = 'revoked', invalidated_time = ?, updated_time = ?
                WHERE user_id = ? AND session_id = ? AND status = 'active'
                """,
                (now_value, now_value, user_id, session_id),
            )
            updated = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id = ? AND user_id = ?",
                (row["id"], user_id),
            ).fetchone()
        return self._subscription_from_row(updated)

    def get_push_subscription(
        self,
        subscription_id: int,
        user_id: int,
    ) -> PushSubscriptionRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM push_subscriptions
                WHERE id = ? AND user_id = ?
                """,
                (subscription_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("push subscription not found")
        return self._subscription_from_row(row)

    def set_push_subscription_status(
        self,
        subscription_id: int,
        user_id: int,
        *,
        status: PushSubscriptionStatus,
        last_error_code: str | None = None,
    ) -> PushSubscriptionRecord:
        if status == PushSubscriptionStatus.ACTIVE:
            raise InvalidOperationError("reactivation requires a new subscription")
        now_value = serialize_utc_datetime(utc_now(), field_name="invalidated_time")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE push_subscriptions
                SET status = ?, invalidated_time = ?, last_error_code = ?,
                    updated_time = ?
                WHERE id = ? AND user_id = ? AND status = 'active'
                """,
                (
                    status.value,
                    now_value,
                    last_error_code[:100] if last_error_code else None,
                    now_value,
                    subscription_id,
                    user_id,
                ),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM push_subscriptions WHERE id = ? AND user_id = ?",
                    (subscription_id, user_id),
                ).fetchone()
                if exists is None:
                    raise NotFoundError("push subscription not found")
                raise InvalidOperationError("push subscription is not active")
            updated = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id = ? AND user_id = ?",
                (subscription_id, user_id),
            ).fetchone()
        return self._subscription_from_row(updated)
