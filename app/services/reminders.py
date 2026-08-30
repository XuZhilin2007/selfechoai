from __future__ import annotations

from datetime import date, datetime

from app.auth_repository import AuthRepository
from app.reminder_repository import ReminderRepository, utc_now
from app.repository import NotFoundError, Repository
from app.schemas import (
    ItemReminderResponse,
    ReminderPromptResponse,
    ReminderPublic,
    ReminderRecord,
    ReminderSettingsPublic,
    ReminderWithItemPublic,
)
from app.time_utils import local_datetime_to_utc


class ReminderService:
    def __init__(
        self,
        reminder_repository: ReminderRepository,
        item_repository: Repository,
        auth_repository: AuthRepository,
    ) -> None:
        self.reminders = reminder_repository
        self.items = item_repository
        self.auth = auth_repository

    @staticmethod
    def _public(record: ReminderRecord) -> ReminderPublic:
        return ReminderPublic(
            id=record.id,
            item_id=record.item_id,
            source_expression=record.source_expression,
            scheduled_timezone=record.scheduled_timezone,
            remind_at=record.remind_at,
            status=record.status,
            created_time=record.created_time,
            updated_time=record.updated_time,
            due_time=record.due_time,
            cancelled_time=record.cancelled_time,
            cancel_reason=record.cancel_reason,
            surfaced_time=record.surfaced_time,
        )

    @staticmethod
    def local_datetime_to_utc(
        local_date: date,
        local_time: str,
        timezone_name: str,
    ) -> datetime:
        return local_datetime_to_utc(local_date, local_time, timezone_name)

    def _user(self, user_id: int):
        user = self.auth.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("user not found")
        return user

    def _scheduled_utc(
        self,
        user_id: int,
        *,
        local_date: date,
        local_time: str | None,
    ) -> tuple[datetime, str]:
        user = self._user(user_id)
        time_value = local_time or user.default_reminder_time
        remind_at = self.local_datetime_to_utc(
            local_date,
            time_value,
            user.timezone,
        )
        if remind_at <= utc_now():
            raise ValueError("reminder date and time must be in the future")
        return remind_at, user.timezone

    def lazy_transition_due(
        self,
        user_id: int,
        *,
        as_of: datetime | None = None,
    ) -> int:
        return self.reminders.mark_due_reminders(user_id, as_of=as_of)

    def get_item_state(self, item_id: int, user_id: int) -> ItemReminderResponse:
        reminder = self.reminders.get_relevant_reminder_for_item(item_id, user_id)
        return ItemReminderResponse(
            reminder=self._public(reminder) if reminder is not None else None,
            show_reminder_prompt=self.reminders.should_offer_reminder_prompt(
                item_id,
                user_id,
            ),
        )

    def create_reminder(
        self,
        item_id: int,
        user_id: int,
        *,
        local_date: date,
        local_time: str | None,
    ) -> ReminderPublic:
        self.items.get_item(item_id, user_id)
        remind_at, timezone_name = self._scheduled_utc(
            user_id,
            local_date=local_date,
            local_time=local_time,
        )
        return self._public(
            self.reminders.create_reminder(
                item_id=item_id,
                user_id=user_id,
                scheduled_timezone=timezone_name,
                remind_at=remind_at,
            )
        )

    def reschedule_reminder(
        self,
        reminder_id: int,
        user_id: int,
        *,
        local_date: date,
        local_time: str | None,
    ) -> ReminderPublic:
        self.reminders.get_reminder(reminder_id, user_id)
        remind_at, timezone_name = self._scheduled_utc(
            user_id,
            local_date=local_date,
            local_time=local_time,
        )
        return self._public(
            self.reminders.reschedule(
                reminder_id,
                user_id,
                remind_at=remind_at,
                scheduled_timezone=timezone_name,
            )
        )

    def cancel_reminder(self, reminder_id: int, user_id: int) -> ReminderPublic:
        return self._public(self.reminders.cancel(reminder_id, user_id))

    def list_upcoming(self, user_id: int) -> list[ReminderWithItemPublic]:
        return [
            ReminderWithItemPublic(
                **self._public(reminder).model_dump(),
                item_title=self.items.get_item(reminder.item_id, user_id).title,
            )
            for reminder in self.reminders.list_upcoming(user_id)
        ]

    def list_due(
        self,
        user_id: int,
        *,
        unsurfaced_only: bool = False,
    ) -> list[ReminderWithItemPublic]:
        return [
            ReminderWithItemPublic(
                **self._public(reminder).model_dump(),
                item_title=self.items.get_item(reminder.item_id, user_id).title,
            )
            for reminder in self.reminders.list_due(
                user_id,
                unsurfaced_only=unsurfaced_only,
            )
        ]

    def mark_surfaced(self, reminder_id: int, user_id: int) -> ReminderPublic:
        return self._public(
            self.reminders.mark_surfaced(
                reminder_id,
                user_id,
                surfaced_time=utc_now(),
            )
        )

    def dismiss_prompt(
        self,
        item_id: int,
        user_id: int,
    ) -> ReminderPromptResponse:
        return ReminderPromptResponse(
            dismissed_time=self.reminders.dismiss_reminder_prompt(
                item_id,
                user_id,
            )
        )

    def get_settings(self, user_id: int) -> ReminderSettingsPublic:
        user = self._user(user_id)
        return ReminderSettingsPublic(
            timezone=user.timezone,
            default_reminder_time=user.default_reminder_time,
        )

    def update_default_time(
        self,
        user_id: int,
        default_reminder_time: str,
    ) -> ReminderSettingsPublic:
        user = self.auth.update_user_profile(
            user_id,
            default_reminder_time=default_reminder_time,
        )
        return ReminderSettingsPublic(
            timezone=user.timezone,
            default_reminder_time=user.default_reminder_time,
        )
