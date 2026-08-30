from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth_repository import AuthRepository
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.reminder_repository import ReminderRepository
from app.repository import InvalidOperationError, Repository
from app.schemas import (
    AIItemFields,
    InputMethod,
    ItemStatus,
    PushSubscriptionStatus,
    ReminderDeliveryStatus,
    ReminderStatus,
    UserItemPatch,
    WebPushOutcome,
)
from app.services.reminder_delivery import (
    ReminderDeliveryService,
    ReminderSweepResult,
    run_reminder_polling_worker,
)
from app.services.web_push import WebPushResult
from tests.conftest import FunctionAIService


REMIND_AT = datetime(2035, 1, 2, 3, 4, tzinfo=timezone.utc)
SWEEP_AT = REMIND_AT + timedelta(seconds=1)
SESSION_EXPIRES = datetime(2040, 1, 1, tzinfo=timezone.utc)


class RecordingWebPushService:
    def __init__(self, result: WebPushResult):
        self.result = result
        self.calls: list[tuple[int, dict[str, object]]] = []
        self._lock = threading.Lock()

    def send(self, subscription, payload):
        with self._lock:
            self.calls.append((subscription.id, payload.model_dump()))
        return self.result


def create_item(repository: Repository, user_id: int, title: str):
    item_input = repository.create_input(title, InputMethod.TEXT, user_id)
    assert repository.claim_input(item_input.id, user_id)
    return repository.create_item_from_input(
        item_input.id,
        user_id,
        AIItemFields(title=title, type="note", status=ItemStatus.ACTIVE),
        set(),
    )


@pytest.fixture
def delivery_context(tmp_path: Path):
    database = Database(tmp_path / "delivery-worker.db")
    database.initialize()
    auth = AuthRepository(database)
    user = auth.create_user(
        email="delivery@example.com",
        password_hash="delivery-password-hash",
        display_name="Delivery Owner",
        timezone_name="Asia/Shanghai",
    )
    other_user = auth.create_user(
        email="other-delivery@example.com",
        password_hash="other-delivery-password-hash",
        display_name="Other Delivery Owner",
        timezone_name="Europe/London",
    )
    session = auth.create_session(
        user_id=user.id,
        token_hash="delivery-session",
        csrf_token_hash="delivery-csrf",
        expires_time=SESSION_EXPIRES,
    )
    other_session = auth.create_session(
        user_id=other_user.id,
        token_hash="other-delivery-session",
        csrf_token_hash="other-delivery-csrf",
        expires_time=SESSION_EXPIRES,
    )
    items = Repository(database)
    reminders = ReminderRepository(database)
    item = create_item(items, user.id, "自动提醒事项")
    return {
        "database": database,
        "auth": auth,
        "user": user,
        "other_user": other_user,
        "session": session,
        "other_session": other_session,
        "items": items,
        "reminders": reminders,
        "item": item,
    }


def create_reminder(context, *, item=None, remind_at=REMIND_AT):
    return context["reminders"].create_reminder(
        item_id=(item or context["item"]).id,
        user_id=context["user"].id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=remind_at,
    )


def create_subscription(
    context,
    *,
    session=None,
    suffix="active",
    user=None,
):
    owner = user or context["user"]
    bound_session = session or context["session"]
    return context["reminders"].sync_push_subscription(
        user_id=owner.id,
        session_id=bound_session.id,
        endpoint=f"https://push.example.test/{suffix}",
        p256dh=f"p256dh-{suffix}",
        auth=f"auth-{suffix}",
    )


def delivery_rows(context):
    with context["database"].connection() as connection:
        return connection.execute(
            "SELECT * FROM reminder_deliveries ORDER BY id"
        ).fetchall()


def test_due_claim_future_repeated_and_batch_limit(delivery_context):
    context = delivery_context
    future = create_reminder(
        context,
        remind_at=REMIND_AT + timedelta(days=1),
    )
    first_due = create_reminder(
        context,
        item=create_item(context["items"], context["user"].id, "Due 1"),
    )
    second_due = create_reminder(
        context,
        item=create_item(context["items"], context["user"].id, "Due 2"),
    )
    third_due = create_reminder(
        context,
        item=create_item(context["items"], context["user"].id, "Due 3"),
    )

    first_batch = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=2,
    )
    second_batch = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=2,
    )
    repeated = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=2,
    )

    assert first_batch.claimed_reminder_ids == (first_due.id, second_due.id)
    assert second_batch.claimed_reminder_ids == (third_due.id,)
    assert repeated.claimed_reminder_ids == ()
    assert context["reminders"].get_reminder(
        future.id, context["user"].id
    ).status == ReminderStatus.SCHEDULED


def test_claim_filters_session_subscription_user_and_account_eligibility(
    delivery_context,
):
    context = delivery_context
    reminder = create_reminder(context)
    active_a = create_subscription(context, suffix="active-a")
    active_b_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="delivery-session-b",
        csrf_token_hash="delivery-csrf-b",
        expires_time=SESSION_EXPIRES,
    )
    active_b = create_subscription(
        context,
        session=active_b_session,
        suffix="active-b",
    )
    revoked_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="delivery-session-revoked",
        csrf_token_hash="delivery-csrf-revoked",
        expires_time=SESSION_EXPIRES,
    )
    revoked = create_subscription(
        context,
        session=revoked_session,
        suffix="revoked-session",
    )
    context["auth"].revoke_session(revoked_session.id)
    expired_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="delivery-session-expired",
        csrf_token_hash="delivery-csrf-expired",
        expires_time=SWEEP_AT - timedelta(seconds=1),
    )
    expired = create_subscription(
        context,
        session=expired_session,
        suffix="expired-session",
    )
    invalid_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="delivery-session-invalid",
        csrf_token_hash="delivery-csrf-invalid",
        expires_time=SESSION_EXPIRES,
    )
    invalid = create_subscription(
        context,
        session=invalid_session,
        suffix="invalid",
    )
    context["reminders"].set_push_subscription_status(
        invalid.id,
        context["user"].id,
        status=PushSubscriptionStatus.INVALID,
        last_error_code="http_410",
    )
    other = create_subscription(
        context,
        user=context["other_user"],
        session=context["other_session"],
        suffix="other-account",
    )

    claimed = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
    )
    targets = {row["subscription_id"] for row in delivery_rows(context)}

    assert claimed.claimed_reminder_ids == (reminder.id,)
    assert targets == {active_a.id, active_b.id}
    assert {revoked.id, expired.id, invalid.id, other.id}.isdisjoint(targets)


def test_disabled_account_has_no_delivery_target(delivery_context):
    context = delivery_context
    reminder = create_reminder(context)
    create_subscription(context)
    with context["database"].transaction() as connection:
        connection.execute(
            "UPDATE users SET status = 'disabled' WHERE id = ?",
            (context["user"].id,),
        )

    result = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
    )

    assert result.claimed_reminder_ids == (reminder.id,)
    assert result.queued_delivery_ids == ()
    assert delivery_rows(context) == []


def test_no_subscription_and_lazy_due_create_no_delivery(delivery_context):
    context = delivery_context
    first = create_reminder(context)
    result = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
    )
    assert result.claimed_reminder_ids == (first.id,)
    assert result.queued_delivery_ids == ()

    second = create_reminder(
        context,
        item=create_item(context["items"], context["user"].id, "Lazy due"),
    )
    create_subscription(context)
    assert context["reminders"].mark_due_reminders(
        context["user"].id,
        as_of=SWEEP_AT,
    ) == 1
    assert context["reminders"].get_reminder(
        second.id,
        context["user"].id,
    ).status == ReminderStatus.DUE
    assert delivery_rows(context) == []


def test_worker_without_push_marks_due_without_delivery_attempt(delivery_context):
    context = delivery_context
    reminder = create_reminder(context)
    create_subscription(context)
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )

    result = ReminderDeliveryService(
        context["reminders"],
        transport,
        delivery_enabled=False,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT,
        batch_size=10,
        stale_after_seconds=300,
    )

    assert result.claimed_reminders == 1
    assert result.queued_deliveries == 0
    assert result.attempted_deliveries == 0
    assert context["reminders"].get_reminder(
        reminder.id,
        context["user"].id,
    ).status == ReminderStatus.DUE
    assert delivery_rows(context) == []
    assert transport.calls == []


def test_two_repository_instances_claim_one_fanout_set(delivery_context):
    context = delivery_context
    reminder = create_reminder(context)
    subscription = create_subscription(context)
    barrier = threading.Barrier(2)

    def claim(repository: ReminderRepository):
        barrier.wait()
        return repository.claim_due_reminders(
            as_of=SWEEP_AT,
            batch_size=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                claim,
                [
                    ReminderRepository(context["database"]),
                    ReminderRepository(context["database"]),
                ],
            )
        )

    assert sum(len(result.claimed_reminder_ids) for result in results) == 1
    rows = delivery_rows(context)
    assert len(rows) == 1
    assert rows[0]["reminder_id"] == reminder.id
    assert rows[0]["subscription_id"] == subscription.id


def test_two_sweeps_attempt_one_queued_delivery_once(delivery_context):
    context = delivery_context
    create_reminder(context)
    create_subscription(context)
    context["reminders"].claim_due_reminders(as_of=SWEEP_AT, batch_size=10)
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )
    barrier = threading.Barrier(2)

    def sweep(repository: ReminderRepository):
        barrier.wait()
        return ReminderDeliveryService(repository, transport).run_reminder_sweep_once(
            now_utc=SWEEP_AT,
            batch_size=10,
            stale_after_seconds=300,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                sweep,
                [
                    ReminderRepository(context["database"]),
                    ReminderRepository(context["database"]),
                ],
            )
        )

    assert sum(result.attempted_deliveries for result in results) == 1
    assert len(transport.calls) == 1
    assert delivery_rows(context)[0]["status"] == ReminderDeliveryStatus.SENT.value


def test_delivery_pair_is_unique(delivery_context):
    context = delivery_context
    reminder = create_reminder(context)
    subscription = create_subscription(context)
    context["reminders"].claim_due_reminders(as_of=SWEEP_AT, batch_size=10)

    with pytest.raises(InvalidOperationError, match="delivery already exists"):
        context["reminders"].create_delivery(
            reminder_id=reminder.id,
            subscription_id=subscription.id,
            user_id=context["user"].id,
        )


def test_sending_state_is_committed_before_transport(delivery_context):
    context = delivery_context
    create_reminder(context)
    create_subscription(context)

    class InspectingTransport:
        def __init__(self):
            self.status_seen: str | None = None

        def send(self, _subscription, _payload):
            self.status_seen = delivery_rows(context)[0]["status"]
            return WebPushResult(WebPushOutcome.ACCEPTED, 201)

    transport = InspectingTransport()
    ReminderDeliveryService(
        context["reminders"],
        transport,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT,
        batch_size=10,
        stale_after_seconds=300,
    )

    assert transport.status_seen == ReminderDeliveryStatus.SENDING.value
    assert delivery_rows(context)[0]["status"] == ReminderDeliveryStatus.SENT.value


@pytest.mark.parametrize(
    (
        "outcome",
        "provider_status",
        "error_code",
        "expected_subscription_status",
    ),
    [
        (WebPushOutcome.ACCEPTED, 201, None, PushSubscriptionStatus.ACTIVE),
        (
            WebPushOutcome.SUBSCRIPTION_GONE,
            404,
            "http_404",
            PushSubscriptionStatus.INVALID,
        ),
        (
            WebPushOutcome.SUBSCRIPTION_GONE,
            410,
            "http_410",
            PushSubscriptionStatus.INVALID,
        ),
        (
            WebPushOutcome.AUTHENTICATION_ERROR,
            401,
            "http_401",
            PushSubscriptionStatus.ACTIVE,
        ),
        (
            WebPushOutcome.TRANSIENT_ERROR,
            503,
            "http_503",
            PushSubscriptionStatus.ACTIVE,
        ),
        (
            WebPushOutcome.TRANSIENT_ERROR,
            None,
            "timeout",
            PushSubscriptionStatus.ACTIVE,
        ),
        (
            WebPushOutcome.TRANSIENT_ERROR,
            None,
            "network_error",
            PushSubscriptionStatus.ACTIVE,
        ),
    ],
)
def test_delivery_outcomes_are_terminal_without_retry(
    delivery_context,
    outcome,
    provider_status,
    error_code,
    expected_subscription_status,
):
    context = delivery_context
    create_reminder(context)
    subscription = create_subscription(context)
    transport = RecordingWebPushService(
        WebPushResult(outcome, provider_status, error_code)
    )
    service = ReminderDeliveryService(context["reminders"], transport)

    first = service.run_reminder_sweep_once(
        now_utc=SWEEP_AT,
        batch_size=10,
        stale_after_seconds=300,
    )
    second = service.run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(minutes=1),
        batch_size=10,
        stale_after_seconds=300,
    )

    delivery = delivery_rows(context)[0]
    stored_subscription = context["reminders"].get_push_subscription(
        subscription.id,
        context["user"].id,
    )
    expected_delivery_status = (
        ReminderDeliveryStatus.SENT
        if outcome == WebPushOutcome.ACCEPTED
        else ReminderDeliveryStatus.FAILED
    )
    assert first.attempted_deliveries == 1
    assert second.attempted_deliveries == 0
    assert len(transport.calls) == 1
    assert delivery["status"] == expected_delivery_status.value
    assert stored_subscription.status == expected_subscription_status
    assert set(transport.calls[0][1]) == {"type", "title", "body", "target_path"}


def test_multiple_devices_are_independent_when_one_transport_raises(
    delivery_context,
    caplog,
):
    context = delivery_context
    create_reminder(context)
    first = create_subscription(context, suffix="private-first-endpoint")
    second_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="delivery-isolation-session",
        csrf_token_hash="delivery-isolation-csrf",
        expires_time=SESSION_EXPIRES,
    )
    second = create_subscription(
        context,
        session=second_session,
        suffix="private-second-endpoint",
    )

    class IsolatedTransport:
        def __init__(self):
            self.calls: list[int] = []

        def send(self, subscription, _payload):
            self.calls.append(subscription.id)
            if subscription.id == first.id:
                raise RuntimeError(subscription.endpoint.get_secret_value())
            return WebPushResult(WebPushOutcome.ACCEPTED, 201)

    transport = IsolatedTransport()
    ReminderDeliveryService(
        context["reminders"],
        transport,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT,
        batch_size=10,
        stale_after_seconds=300,
    )

    rows = delivery_rows(context)
    assert transport.calls == [first.id, second.id]
    assert [row["status"] for row in rows] == ["failed", "sent"]
    assert "private-first-endpoint" not in caplog.text
    assert "private-second-endpoint" not in caplog.text


def test_queued_delivery_survives_restart_and_executes_once(delivery_context):
    context = delivery_context
    create_reminder(context)
    create_subscription(context)
    claim = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
    )
    assert len(claim.queued_delivery_ids) == 1
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )
    restarted = ReminderDeliveryService(
        ReminderRepository(context["database"]),
        transport,
    )

    restarted.run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(seconds=1),
        batch_size=10,
        stale_after_seconds=300,
    )
    restarted.run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(seconds=2),
        batch_size=10,
        stale_after_seconds=300,
    )

    assert delivery_rows(context)[0]["status"] == "sent"
    assert len(transport.calls) == 1


def test_sending_before_outbound_becomes_unknown_and_is_never_sent(
    delivery_context,
):
    context = delivery_context
    create_reminder(context)
    create_subscription(context)
    context["reminders"].claim_due_reminders(as_of=SWEEP_AT, batch_size=10)
    claimed = context["reminders"].claim_next_queued_delivery(as_of=SWEEP_AT)
    assert claimed is not None
    assert delivery_rows(context)[0]["status"] == "sending"
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )

    result = ReminderDeliveryService(
        ReminderRepository(context["database"]),
        transport,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(seconds=301),
        batch_size=10,
        stale_after_seconds=300,
    )

    assert result.stale_unknown_deliveries == 1
    assert delivery_rows(context)[0]["status"] == "unknown"
    assert transport.calls == []


def test_provider_acceptance_before_finish_commit_stays_unknown(
    delivery_context,
    monkeypatch,
):
    context = delivery_context
    create_reminder(context)
    create_subscription(context)
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )
    service = ReminderDeliveryService(context["reminders"], transport)

    def fail_finish(*_args, **_kwargs):
        raise RuntimeError("simulated finish commit crash")

    monkeypatch.setattr(context["reminders"], "finish_delivery", fail_finish)
    service.run_reminder_sweep_once(
        now_utc=SWEEP_AT,
        batch_size=10,
        stale_after_seconds=300,
    )
    assert len(transport.calls) == 1
    assert delivery_rows(context)[0]["status"] == "sending"

    restarted_transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )
    ReminderDeliveryService(
        ReminderRepository(context["database"]),
        restarted_transport,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(seconds=301),
        batch_size=10,
        stale_after_seconds=300,
    )

    assert delivery_rows(context)[0]["status"] == "unknown"
    assert restarted_transport.calls == []


def test_crash_before_due_claim_commit_leaves_reminder_recoverable(
    delivery_context,
):
    context = delivery_context
    reminder = create_reminder(context)
    create_subscription(context)
    with pytest.raises(RuntimeError, match="simulated claim crash"):
        with context["database"].transaction() as connection:
            connection.execute(
                """
                UPDATE reminders
                SET status = 'due', due_time = ?, updated_time = ?
                WHERE id = ?
                """,
                (SWEEP_AT.isoformat(), SWEEP_AT.isoformat(), reminder.id),
            )
            raise RuntimeError("simulated claim crash")

    assert context["reminders"].get_reminder(
        reminder.id,
        context["user"].id,
    ).status == ReminderStatus.SCHEDULED
    result = context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
    )
    assert result.claimed_reminder_ids == (reminder.id,)


def test_due_history_is_not_cancelled_when_item_completes(delivery_context):
    context = delivery_context
    reminder = create_reminder(context)
    context["reminders"].claim_due_reminders(
        as_of=SWEEP_AT,
        batch_size=10,
        queue_deliveries=False,
    )

    context["items"].update_item(
        context["item"].id,
        context["user"].id,
        UserItemPatch(status=ItemStatus.COMPLETED),
    )

    stored = context["reminders"].get_reminder(
        reminder.id,
        context["user"].id,
    )
    assert stored.status == ReminderStatus.DUE
    assert stored.cancel_reason is None


def test_queued_target_revoked_before_send_fails_without_transport(
    delivery_context,
):
    context = delivery_context
    create_reminder(context)
    subscription = create_subscription(context)
    context["reminders"].claim_due_reminders(as_of=SWEEP_AT, batch_size=10)
    context["reminders"].revoke_push_subscription(
        subscription.id,
        context["user"].id,
        context["session"].id,
    )
    transport = RecordingWebPushService(
        WebPushResult(WebPushOutcome.ACCEPTED, 201)
    )

    result = ReminderDeliveryService(
        context["reminders"],
        transport,
    ).run_reminder_sweep_once(
        now_utc=SWEEP_AT + timedelta(seconds=1),
        batch_size=10,
        stale_after_seconds=300,
    )

    assert result.inactive_target_deliveries == 1
    assert delivery_rows(context)[0]["status"] == "failed"
    assert delivery_rows(context)[0]["attempted_time"] is None
    assert transport.calls == []


def test_worker_disabled_by_default_and_enabled_shutdown_is_clean(tmp_path: Path):
    disabled_app = create_app(
        settings=Settings(
            database_path=tmp_path / "worker-disabled.db",
            session_cookie_secure=False,
        ),
        ai_service=FunctionAIService(lambda text, existing: None),
    )
    with TestClient(disabled_app):
        assert disabled_app.state.reminder_worker_task is None

    enabled_app = create_app(
        settings=Settings(
            database_path=tmp_path / "worker-enabled.db",
            session_cookie_secure=False,
            reminder_worker_enabled=True,
            reminder_poll_interval_seconds=3_600,
        ),
        ai_service=FunctionAIService(lambda text, existing: None),
    )
    with TestClient(enabled_app):
        worker_task = enabled_app.state.reminder_worker_task
        assert worker_task is not None
        assert not worker_task.done()

    assert worker_task.done()
    assert worker_task.cancelled()


def test_worker_loop_continues_after_one_sweep_failure(tmp_path: Path):
    completed_second_sweep = threading.Event()

    class FlakyService:
        def __init__(self):
            self.calls = 0

        def run_reminder_sweep_once(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic sweep failure")
            completed_second_sweep.set()
            return ReminderSweepResult(0, 0, 0, 0, 0, 0, 0)

    service = FlakyService()
    settings = Settings(
        database_path=tmp_path / "loop.db",
        reminder_poll_interval_seconds=0.01,
        reminder_batch_size=1,
        reminder_sending_stale_seconds=1,
    )

    async def exercise() -> None:
        task = asyncio.create_task(run_reminder_polling_worker(service, settings))
        completed = await asyncio.to_thread(completed_second_sweep.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert completed

    asyncio.run(exercise())
    assert service.calls >= 2
