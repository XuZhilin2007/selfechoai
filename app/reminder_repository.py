from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.database import Database
from app.repository import InvalidOperationError, NotFoundError
from app.schemas import ReminderCancelReason, ReminderRecord, ReminderStatus
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

    def list_due(self, user_id: int) -> list[ReminderRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'due'
                ORDER BY due_time ASC, id ASC
                """,
                (user_id,),
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
