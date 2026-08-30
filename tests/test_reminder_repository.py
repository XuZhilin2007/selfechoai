from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.repository as item_repository_module
from app.auth_repository import AuthRepository
from app.database import Database
from app.reminder_repository import ReminderRepository, utc_now
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.schemas import (
    AIItemFields,
    InputMethod,
    ItemStatus,
    ReminderCancelReason,
    ReminderStatus,
    UserItemPatch,
    UserRecord,
)


@dataclass(frozen=True, slots=True)
class ReminderContext:
    database: Database
    auth: AuthRepository
    items: Repository
    reminders: ReminderRepository
    user_a: UserRecord
    user_b: UserRecord
    item_a_id: int
    item_b_id: int


def future_time(*, days: int = 30, minutes: int = 0) -> datetime:
    return utc_now() + timedelta(days=days, minutes=minutes)


def create_item(repository: Repository, user_id: int, title: str) -> int:
    item_input = repository.create_input(title, InputMethod.TEXT, user_id)
    assert repository.claim_input(item_input.id, user_id)
    item = repository.create_item_from_input(
        item_input.id,
        user_id,
        AIItemFields(title=title, type="note", status=ItemStatus.ACTIVE),
        set(),
    )
    return item.id


@pytest.fixture
def reminder_context(tmp_path: Path) -> ReminderContext:
    database = Database(tmp_path / "reminder-domain.db")
    database.initialize()
    auth = AuthRepository(database)
    user_a = auth.create_user(
        email="reminder-a@example.com",
        password_hash="$argon2id$synthetic-reminder-a",
        display_name="Reminder A",
        timezone_name="Asia/Shanghai",
    )
    user_b = auth.create_user(
        email="reminder-b@example.com",
        password_hash="$argon2id$synthetic-reminder-b",
        display_name="Reminder B",
        timezone_name="Europe/London",
    )
    items = Repository(database)
    return ReminderContext(
        database=database,
        auth=auth,
        items=items,
        reminders=ReminderRepository(database),
        user_a=user_a,
        user_b=user_b,
        item_a_id=create_item(items, user_a.id, "A 的事项"),
        item_b_id=create_item(items, user_b.id, "B 的事项"),
    )


def test_create_read_history_active_uniqueness_and_state_persistence(
    reminder_context: ReminderContext,
):
    context = reminder_context
    local_future = future_time(days=60).astimezone(timezone(timedelta(hours=8)))
    scheduled = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=local_future,
        source_expression="  下个月提醒我  ",
    )

    assert scheduled.status == ReminderStatus.SCHEDULED
    assert scheduled.remind_at == local_future.astimezone(timezone.utc)
    assert scheduled.source_expression == "下个月提醒我"
    assert context.reminders.get_reminder(
        scheduled.id,
        context.user_a.id,
    ) == scheduled
    assert context.reminders.list_upcoming(
        context.user_a.id,
        as_of=utc_now(),
    ) == [scheduled]

    with pytest.raises(InvalidOperationError, match="active reminder"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_a.id,
            scheduled_timezone="Asia/Shanghai",
            source_expression="时间待确认",
        )

    cancelled = context.reminders.cancel(scheduled.id, context.user_a.id)
    assert cancelled.status == ReminderStatus.CANCELLED
    assert cancelled.cancel_reason == ReminderCancelReason.USER_CANCELLED
    assert cancelled.cancelled_time is not None

    needs_confirmation = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        source_expression="以后提醒我",
    )
    assert needs_confirmation.status == ReminderStatus.NEEDS_CONFIRMATION
    assert needs_confirmation.remind_at is None
    assert context.reminders.get_current_reminder_for_item(
        context.item_a_id,
        context.user_a.id,
    ) == needs_confirmation
    assert context.reminders.get_relevant_reminder_for_item(
        context.item_a_id,
        context.user_a.id,
    ) == needs_confirmation
    assert context.reminders.get_reminder(
        cancelled.id,
        context.user_a.id,
    ) == cancelled

    next_time = future_time(days=90)
    rescheduled = context.reminders.reschedule(
        needs_confirmation.id,
        context.user_a.id,
        remind_at=next_time,
        scheduled_timezone="Asia/Shanghai",
    )
    due = context.reminders.mark_due(
        rescheduled.id,
        context.user_a.id,
        due_time=next_time + timedelta(seconds=1),
    )
    assert due.status == ReminderStatus.DUE
    assert due.due_time == next_time + timedelta(seconds=1)
    assert context.reminders.list_due(context.user_a.id) == [due]

    newest = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=future_time(days=120),
    )
    assert context.reminders.get_relevant_reminder_for_item(
        context.item_a_id,
        context.user_a.id,
    ) == newest
    assert context.reminders.get_reminder(due.id, context.user_a.id) == due


def test_all_reminder_operations_enforce_per_user_ownership(
    reminder_context: ReminderContext,
):
    context = reminder_context
    reminder_a = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=future_time(days=30),
    )
    reminder_b = context.reminders.create_reminder(
        item_id=context.item_b_id,
        user_id=context.user_b.id,
        scheduled_timezone="Europe/London",
        remind_at=future_time(days=31),
    )

    with pytest.raises(NotFoundError, match="reminder not found"):
        context.reminders.get_reminder(reminder_a.id, context.user_b.id)
    with pytest.raises(NotFoundError, match="reminder not found"):
        context.reminders.reschedule(
            reminder_a.id,
            context.user_b.id,
            remind_at=future_time(days=40),
            scheduled_timezone="Europe/London",
        )
    with pytest.raises(NotFoundError, match="reminder not found"):
        context.reminders.cancel(reminder_a.id, context.user_b.id)
    with pytest.raises(NotFoundError, match="reminder not found"):
        context.reminders.mark_due(
            reminder_a.id,
            context.user_b.id,
            due_time=future_time(days=35),
        )
    with pytest.raises(NotFoundError, match="item not found"):
        context.reminders.get_current_reminder_for_item(
            context.item_a_id,
            context.user_b.id,
        )
    with pytest.raises(NotFoundError, match="item not found"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_b.id,
            scheduled_timezone="Europe/London",
            remind_at=future_time(days=45),
        )

    assert context.reminders.list_upcoming(
        context.user_a.id,
        as_of=utc_now(),
    ) == [reminder_a]
    assert context.reminders.list_upcoming(
        context.user_b.id,
        as_of=utc_now(),
    ) == [reminder_b]
    assert context.reminders.get_reminder(
        reminder_a.id,
        context.user_a.id,
    ) == reminder_a


@pytest.mark.parametrize(
    ("initial_status", "item_status", "expected_reason"),
    (
        (
            ReminderStatus.SCHEDULED,
            ItemStatus.COMPLETED,
            ReminderCancelReason.ITEM_COMPLETED,
        ),
        (
            ReminderStatus.NEEDS_CONFIRMATION,
            ItemStatus.COMPLETED,
            ReminderCancelReason.ITEM_COMPLETED,
        ),
        (
            ReminderStatus.SCHEDULED,
            ItemStatus.TRASH,
            ReminderCancelReason.ITEM_TRASHED,
        ),
        (
            ReminderStatus.NEEDS_CONFIRMATION,
            ItemStatus.TRASH,
            ReminderCancelReason.ITEM_TRASHED,
        ),
    ),
)
def test_item_lifecycle_cancels_active_reminder_and_restore_does_not_revive(
    reminder_context: ReminderContext,
    initial_status: ReminderStatus,
    item_status: ItemStatus,
    expected_reason: ReminderCancelReason,
):
    context = reminder_context
    reminder = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=(
            future_time(days=30)
            if initial_status == ReminderStatus.SCHEDULED
            else None
        ),
        source_expression=(
            "以后提醒我"
            if initial_status == ReminderStatus.NEEDS_CONFIRMATION
            else None
        ),
    )

    context.items.update_item(
        context.item_a_id,
        context.user_a.id,
        UserItemPatch(status=item_status),
    )

    cancelled = context.reminders.get_reminder(reminder.id, context.user_a.id)
    assert cancelled.status == ReminderStatus.CANCELLED
    assert cancelled.cancel_reason == expected_reason
    assert cancelled.cancelled_time is not None

    context.items.update_item(
        context.item_a_id,
        context.user_a.id,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    restored_history = context.reminders.get_reminder(
        reminder.id,
        context.user_a.id,
    )
    assert restored_history.status == ReminderStatus.CANCELLED
    assert context.reminders.get_current_reminder_for_item(
        context.item_a_id,
        context.user_a.id,
    ) is None


def test_due_reminder_remains_awareness_history_after_item_lifecycle_changes(
    reminder_context: ReminderContext,
):
    context = reminder_context
    remind_at = future_time(days=30)
    reminder = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=remind_at,
    )
    due = context.reminders.mark_due(
        reminder.id,
        context.user_a.id,
        due_time=remind_at + timedelta(seconds=1),
    )

    for item_status in (
        ItemStatus.COMPLETED,
        ItemStatus.ACTIVE,
        ItemStatus.TRASH,
    ):
        context.items.update_item(
            context.item_a_id,
            context.user_a.id,
            UserItemPatch(status=item_status),
        )
        persisted = context.reminders.get_reminder(due.id, context.user_a.id)
        assert persisted.status == ReminderStatus.DUE
        assert persisted.cancelled_time is None
        assert persisted.cancel_reason is None

    with pytest.raises(InvalidOperationError, match="active reminder"):
        context.reminders.cancel(due.id, context.user_a.id)


def test_item_status_and_active_reminder_cancellation_are_atomic(
    reminder_context: ReminderContext,
    monkeypatch,
):
    context = reminder_context
    reminder = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=future_time(days=30),
    )

    def fail_cancellation(*args, **kwargs):
        raise RuntimeError("forced reminder cancellation failure")

    monkeypatch.setattr(
        item_repository_module,
        "_cancel_active_reminders_for_item",
        fail_cancellation,
    )
    with pytest.raises(RuntimeError, match="forced reminder cancellation failure"):
        context.items.update_item(
            context.item_a_id,
            context.user_a.id,
            UserItemPatch(status=ItemStatus.COMPLETED),
        )

    assert context.items.get_item(
        context.item_a_id,
        context.user_a.id,
    ).status == ItemStatus.ACTIVE
    assert context.reminders.get_reminder(
        reminder.id,
        context.user_a.id,
    ).status == ReminderStatus.SCHEDULED


def test_ai_item_status_update_uses_same_reminder_lifecycle_semantics(
    reminder_context: ReminderContext,
):
    context = reminder_context
    reminder = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        source_expression="完成时无需再提醒",
    )
    item_input = context.items.create_input(
        "这件事已经完成",
        InputMethod.TEXT,
        context.user_a.id,
        item_id=context.item_a_id,
    )
    assert context.items.claim_input(item_input.id, context.user_a.id)

    context.items.apply_ai_update(
        item_input.id,
        context.item_a_id,
        context.user_a.id,
        AIItemFields(status=ItemStatus.COMPLETED),
        set(),
    )

    cancelled = context.reminders.get_reminder(reminder.id, context.user_a.id)
    assert cancelled.status == ReminderStatus.CANCELLED
    assert cancelled.cancel_reason == ReminderCancelReason.ITEM_COMPLETED


def test_permanent_item_delete_cascades_reminder_and_delivery_history(
    reminder_context: ReminderContext,
):
    context = reminder_context
    remind_at = future_time(days=30)
    reminder = context.reminders.create_reminder(
        item_id=context.item_a_id,
        user_id=context.user_a.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=remind_at,
    )
    due = context.reminders.mark_due(
        reminder.id,
        context.user_a.id,
        due_time=remind_at + timedelta(seconds=1),
    )
    session = context.auth.create_session(
        user_id=context.user_a.id,
        token_hash="synthetic-reminder-session-token",
        csrf_token_hash="synthetic-reminder-csrf-token",
        expires_time=future_time(days=180),
    )
    now_value = utc_now().isoformat()
    with context.database.transaction() as connection:
        subscription_cursor = connection.execute(
            """
            INSERT INTO push_subscriptions (
                user_id, session_id, endpoint, p256dh, auth, status,
                created_time, updated_time
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                context.user_a.id,
                session.id,
                "https://push.invalid/synthetic-stage-2",
                "synthetic-p256dh",
                "synthetic-auth",
                now_value,
                now_value,
            ),
        )
        subscription_id = int(subscription_cursor.lastrowid)
        delivery_cursor = connection.execute(
            """
            INSERT INTO reminder_deliveries (
                user_id, reminder_id, subscription_id, status
            ) VALUES (?, ?, ?, 'queued')
            """,
            (
                context.user_a.id,
                due.id,
                subscription_id,
            ),
        )
        delivery_id = int(delivery_cursor.lastrowid)

    context.items.update_item(
        context.item_a_id,
        context.user_a.id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    assert context.reminders.get_reminder(
        due.id,
        context.user_a.id,
    ).status == ReminderStatus.DUE

    context.items.permanently_delete_item(
        context.item_a_id,
        context.user_a.id,
    )

    with pytest.raises(NotFoundError, match="reminder not found"):
        context.reminders.get_reminder(due.id, context.user_a.id)
    with context.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reminders WHERE id = ?",
            (due.id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()[0] == 1


def test_reminder_time_and_timezone_validation(reminder_context: ReminderContext):
    context = reminder_context
    with pytest.raises(ValueError, match="timezone information"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_a.id,
            scheduled_timezone="Asia/Shanghai",
            remind_at=datetime(2035, 1, 1, 9, 0),
        )
    with pytest.raises(ValueError, match="IANA timezone"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_a.id,
            scheduled_timezone="Invalid/Timezone",
            remind_at=future_time(days=30),
        )
    with pytest.raises(ValueError, match="future"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_a.id,
            scheduled_timezone="Asia/Shanghai",
            remind_at=utc_now() - timedelta(seconds=1),
        )

    context.items.update_item(
        context.item_a_id,
        context.user_a.id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    with pytest.raises(InvalidOperationError, match="active items"):
        context.reminders.create_reminder(
            item_id=context.item_a_id,
            user_id=context.user_a.id,
            scheduled_timezone="Asia/Shanghai",
            remind_at=future_time(days=30),
        )
