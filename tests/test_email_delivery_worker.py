from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr

from app.auth_repository import AuthRepository
from app.config import Settings
from app.database import Database
from app.email_repository import EmailReminderRepository
from app.reminder_repository import ReminderRepository
from app.schemas import ItemStatus, UserItemPatch, WebPushOutcome
from app.services.email_reminders import EmailReminderService
from app.services.reminder_delivery import ReminderDeliveryService
from app.services.reminders import ReminderService
from app.services.tencent_ses import (
    EmailSendOutcome,
    EmailSendResult,
    EmailStatusQueryOutcome,
    EmailStatusResult,
)
from app.services.web_push import WebPushResult
from app.repository import Repository
from tests.email_fakes import RecordingEmailSender


REMIND_AT = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)


class RecordingPushSender:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or WebPushResult(WebPushOutcome.ACCEPTED, 201)

    def send(self, subscription, payload):
        self.calls.append((subscription.id, payload))
        return self.result


@pytest.fixture
def delivery_context(tmp_path):
    database = Database(tmp_path / "email-delivery.db")
    database.initialize()
    auth = AuthRepository(database)
    user = auth.create_user(
        email="login@example.com",
        password_hash="hash",
        display_name="Owner",
        timezone_name="Asia/Shanghai",
    )
    session = auth.create_session(
        user_id=user.id,
        token_hash="session",
        csrf_token_hash="csrf",
        expires_time=REMIND_AT + timedelta(days=30),
    )
    with database.transaction() as connection:
        item_cursor = connection.execute(
            """
            INSERT INTO personal_items (
                user_id, title, type, importance, urgency, status,
                created_time, updated_time
            ) VALUES (?, '当前短标题', 'task', 'medium', 'medium', 'active', ?, ?)
            """,
            (user.id, REMIND_AT.isoformat(), REMIND_AT.isoformat()),
        )
    item_id = int(item_cursor.lastrowid)
    settings = Settings(
        database_path=database.path,
        app_origin="http://community.example",
        session_cookie_secure=False,
        email_reminder_provider_enabled=True,
        tencentcloud_secret_id=SecretStr("community-test-id"),
        tencentcloud_secret_key=SecretStr("community-test-key"),
        tencent_ses_from_email_address=(
            "SelfEcho Community <reminder@community.example>"
        ),
        tencent_ses_verification_template_id=101,
        tencent_ses_reminder_template_id=102,
        email_verification_code_pepper=SecretStr("pepper"),
    )
    sender = RecordingEmailSender()
    email_repository = EmailReminderRepository(database)
    email_service = EmailReminderService(email_repository, sender, settings)
    reminder_repository = ReminderRepository(database)
    return {
        "database": database,
        "auth": auth,
        "user": user,
        "session": session,
        "item_id": item_id,
        "items": Repository(database),
        "settings": settings,
        "sender": sender,
        "emails": email_repository,
        "email_service": email_service,
        "reminders": reminder_repository,
    }


def schedule_reminder(context, *, item_id=None, remind_at=REMIND_AT):
    with context["database"].transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO reminders (
                user_id, item_id, scheduled_timezone, remind_at, status,
                created_time, updated_time
            ) VALUES (?, ?, 'Asia/Shanghai', ?, 'scheduled', ?, ?)
            """,
            (
                context["user"].id,
                item_id or context["item_id"],
                remind_at.isoformat(),
                (remind_at - timedelta(hours=1)).isoformat(),
                (remind_at - timedelta(hours=1)).isoformat(),
            ),
        )
    return int(cursor.lastrowid)


def make_email_eligible(context, address="reminder@example.com", enabled=True):
    with context["database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO email_reminder_settings (
                user_id, email_address, verification_status, verified_at,
                enabled, health_status, created_time, updated_time
            ) VALUES (?, ?, 'verified', ?, ?, 'healthy', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                email_address = excluded.email_address,
                verification_status = 'verified',
                verified_at = excluded.verified_at,
                enabled = excluded.enabled,
                health_status = 'healthy', pause_reason = NULL,
                updated_time = excluded.updated_time
            """,
            (
                context["user"].id,
                address,
                REMIND_AT.isoformat(),
                int(enabled),
                REMIND_AT.isoformat(),
                REMIND_AT.isoformat(),
            ),
        )


def email_rows(context):
    with context["database"].connection() as connection:
        return connection.execute(
            "SELECT * FROM reminder_email_deliveries ORDER BY id"
        ).fetchall()


def run_email(context, when):
    return context["email_service"].run_sweep_once(
        now_utc=when,
        batch_size=20,
        stale_after_seconds=300,
    )


def test_off_at_due_never_backfills_when_enabled_after_due(delivery_context):
    context = delivery_context
    reminder_id = schedule_reminder(context)
    make_email_eligible(context, enabled=False)

    claim = context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    context["emails"].set_enabled(
        context["user"].id, True, changed_at=REMIND_AT + timedelta(minutes=5)
    )
    repeated = context["reminders"].claim_due_reminders(
        as_of=REMIND_AT + timedelta(minutes=5)
    )

    assert claim.claimed_reminder_ids == (reminder_id,)
    assert claim.queued_email_delivery_ids == ()
    assert repeated.claimed_reminder_ids == ()
    assert email_rows(context) == []


def test_on_at_due_enqueues_exactly_one_email_independent_of_push_fanout(
    delivery_context,
):
    context = delivery_context
    reminder_id = schedule_reminder(context)
    make_email_eligible(context)
    first = context["reminders"].sync_push_subscription(
        user_id=context["user"].id,
        session_id=context["session"].id,
        endpoint="https://push.example/first",
        p256dh="first-key",
        auth="first-auth",
    )
    second_session = context["auth"].create_session(
        user_id=context["user"].id,
        token_hash="session-2",
        csrf_token_hash="csrf-2",
        expires_time=REMIND_AT + timedelta(days=30),
    )
    second = context["reminders"].sync_push_subscription(
        user_id=context["user"].id,
        session_id=second_session.id,
        endpoint="https://push.example/second",
        p256dh="second-key",
        auth="second-auth",
    )

    claim = context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    repeated = context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    with context["database"].connection() as connection:
        push_rows = connection.execute(
            "SELECT * FROM reminder_deliveries ORDER BY id"
        ).fetchall()

    assert claim.claimed_reminder_ids == (reminder_id,)
    assert len(claim.queued_email_delivery_ids) == 1
    assert {row["subscription_id"] for row in push_rows} == {first.id, second.id}
    assert len(email_rows(context)) == 1
    assert repeated.queued_email_delivery_ids == ()


def test_concurrent_due_claim_creates_one_email_row(delivery_context):
    context = delivery_context
    reminder_id = schedule_reminder(context)
    make_email_eligible(context)
    barrier = threading.Barrier(2)

    def claim(_index):
        barrier.wait()
        return ReminderRepository(context["database"]).claim_due_reminders(
            as_of=REMIND_AT
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, range(2)))
    assert sum(reminder_id in result.claimed_reminder_ids for result in results) == 1
    assert len(email_rows(context)) == 1


@pytest.mark.parametrize(
    ("push_enabled", "email_enabled"),
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_request_time_lazy_due_uses_independent_channel_gates(
    delivery_context,
    push_enabled,
    email_enabled,
):
    context = delivery_context
    reminder_id = schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].sync_push_subscription(
        user_id=context["user"].id,
        session_id=context["session"].id,
        endpoint="https://push.example/lazy-due",
        p256dh="lazy-key",
        auth="lazy-auth",
    )
    reminder_service = ReminderService(
        context["reminders"],
        context["items"],
        context["auth"],
        push_delivery_enabled=push_enabled,
        email_delivery_enabled=email_enabled,
    )

    assert reminder_service.lazy_transition_due(
        context["user"].id, as_of=REMIND_AT
    ) == 1
    with context["database"].connection() as connection:
        push_count = connection.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE reminder_id = ?",
            (reminder_id,),
        ).fetchone()[0]
    assert push_count == int(push_enabled)
    assert len(email_rows(context)) == int(email_enabled)
    if email_enabled:
        assert email_rows(context)[0]["reminder_id"] == reminder_id
    assert reminder_service.lazy_transition_due(
        context["user"].id, as_of=REMIND_AT + timedelta(minutes=1)
    ) == 0
    with context["database"].connection() as connection:
        repeated_push_count = connection.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE reminder_id = ?",
            (reminder_id,),
        ).fetchone()[0]
    assert repeated_push_count == int(push_enabled)
    assert len(email_rows(context)) == int(email_enabled)


def test_two_email_workers_claim_one_provider_attempt(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    barrier = threading.Barrier(2)

    def sweep(_index):
        barrier.wait()
        service = EmailReminderService(
            EmailReminderRepository(context["database"]),
            context["sender"],
            context["settings"],
        )
        return service.run_sweep_once(
            now_utc=REMIND_AT,
            batch_size=10,
            stale_after_seconds=300,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(sweep, range(2)))
    assert len(context["sender"].reminder_calls) == 1
    assert email_rows(context)[0]["status"] == "accepted"


@pytest.mark.parametrize(
    (
        "push_result",
        "email_result",
        "expected_push_sent",
        "expected_push_failed",
        "expected_email_accepted",
        "expected_email_retry",
    ),
    [
        (
            WebPushResult(WebPushOutcome.ACCEPTED, 201),
            EmailSendResult(
                EmailSendOutcome.RETRYABLE_FAILURE,
                error_code="FailedOperation.FrequencyLimit",
            ),
            1,
            0,
            0,
            1,
        ),
        (
            WebPushResult(WebPushOutcome.PROVIDER_ERROR, 500),
            EmailSendResult(
                EmailSendOutcome.ACCEPTED,
                provider_message_id="independent-email",
                provider_request_id="independent-request",
                provider_request_date="2026-09-04",
            ),
            0,
            1,
            1,
            0,
        ),
    ],
)
def test_push_and_email_outcomes_do_not_trigger_or_suppress_each_other(
    delivery_context,
    push_result,
    email_result,
    expected_push_sent,
    expected_push_failed,
    expected_email_accepted,
    expected_email_retry,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].sync_push_subscription(
        user_id=context["user"].id,
        session_id=context["session"].id,
        endpoint="https://push.example/independent",
        p256dh="independent-key",
        auth="independent-auth",
    )
    context["sender"].queue_send_results(email_result)
    push_sender = RecordingPushSender(push_result)

    result = ReminderDeliveryService(
        context["reminders"],
        push_sender,
        context["email_service"],
        delivery_enabled=True,
        email_delivery_enabled=True,
    ).run_reminder_sweep_once(
        now_utc=REMIND_AT,
        batch_size=10,
        stale_after_seconds=300,
    )

    assert len(push_sender.calls) == 1
    assert len(context["sender"].reminder_calls) == 1
    assert result.sent_deliveries == expected_push_sent
    assert result.failed_deliveries == expected_push_failed
    assert result.email_accepted == expected_email_accepted
    assert result.email_retry_wait == expected_email_retry


def test_accepted_send_uses_generic_dashboard_link_without_item_content(
    delivery_context,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)

    result = run_email(context, REMIND_AT)

    row = email_rows(context)[0]
    assert result.attempted == 1
    assert result.accepted == 1
    assert context["sender"].reminder_calls == [
        ("reminder@example.com", "http://community.example/dashboard")
    ]
    assert row["status"] == "accepted"
    assert row["provider_message_id"] == "message-1"
    assert row["provider_request_id"] == "request-1"
    assert row["provider_request_date"] == "2026-09-04"
    assert row["provider_delivery_status"] == "pending"


def test_retry_schedule_is_0_1_4_10_minutes_and_four_attempts_max(
    delivery_context,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    transient = EmailSendResult(
        EmailSendOutcome.RETRYABLE_FAILURE,
        error_code="FailedOperation.FrequencyLimit",
        provider_request_date="2026-09-04",
    )
    accepted = EmailSendResult(
        EmailSendOutcome.ACCEPTED,
        provider_message_id="accepted-fourth",
        provider_request_id="request-fourth",
        provider_request_date="2026-09-04",
    )
    context["sender"].queue_send_results(transient, transient, transient, accepted)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)

    run_email(context, REMIND_AT)
    assert email_rows(context)[0]["next_attempt_at"] == (
        REMIND_AT + timedelta(minutes=1)
    ).isoformat()
    run_email(context, REMIND_AT + timedelta(seconds=59))
    assert len(context["sender"].reminder_calls) == 1
    run_email(context, REMIND_AT + timedelta(minutes=1))
    assert email_rows(context)[0]["next_attempt_at"] == (
        REMIND_AT + timedelta(minutes=4)
    ).isoformat()
    run_email(context, REMIND_AT + timedelta(minutes=4))
    assert email_rows(context)[0]["next_attempt_at"] == (
        REMIND_AT + timedelta(minutes=10)
    ).isoformat()
    run_email(context, REMIND_AT + timedelta(minutes=10))

    row = email_rows(context)[0]
    assert row["status"] == "accepted"
    assert row["attempt_count"] == 4
    assert len(context["sender"].reminder_calls) == 4


def test_fourth_transient_failure_is_terminal(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    transient = EmailSendResult(
        EmailSendOutcome.RETRYABLE_FAILURE,
        error_code="FailedOperation.FrequencyLimit",
        provider_request_date="2026-09-04",
    )
    context["sender"].queue_send_results(*([transient] * 4))
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    for when in (0, 1, 4, 10):
        run_email(context, REMIND_AT + timedelta(minutes=when))
    assert email_rows(context)[0]["status"] == "failed"
    assert len(context["sender"].reminder_calls) == 4


def test_exact_fifteen_minutes_is_expired_without_provider_call(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(
        as_of=REMIND_AT + timedelta(minutes=15)
    )
    run_email(context, REMIND_AT + timedelta(minutes=15))
    assert email_rows(context)[0]["status"] == "expired"
    assert context["sender"].reminder_calls == []


def test_ambiguous_and_stale_sending_are_never_retried(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    ambiguous = EmailSendResult(
        EmailSendOutcome.AMBIGUOUS_FAILURE,
        error_code="transport_ambiguous",
    )
    context["sender"].queue_send_results(ambiguous)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    run_email(context, REMIND_AT + timedelta(minutes=1))
    assert email_rows(context)[0]["status"] == "unknown"
    assert len(context["sender"].reminder_calls) == 1

    with context["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE reminder_email_deliveries
            SET status='sending', last_attempted_at=?, finished_at=NULL
            """,
            ((REMIND_AT + timedelta(minutes=2)).isoformat(),),
        )
    run_email(context, REMIND_AT + timedelta(minutes=8))
    assert email_rows(context)[0]["status"] == "unknown"
    assert len(context["sender"].reminder_calls) == 1


@pytest.mark.parametrize("new_status", [ItemStatus.COMPLETED, ItemStatus.TRASH])
def test_item_completed_or_trashed_before_send_is_suppressed(
    delivery_context, new_status
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    context["items"].update_item(
        context["item_id"],
        context["user"].id,
        UserItemPatch(status=new_status),
    )
    run_email(context, REMIND_AT + timedelta(seconds=1))
    assert email_rows(context)[0]["status"] == "suppressed"
    assert context["sender"].reminder_calls == []


def test_reminder_no_longer_due_before_send_is_suppressed(delivery_context):
    context = delivery_context
    reminder_id = schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    with context["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE reminders
            SET status='cancelled', cancelled_time=?,
                cancel_reason='user_cancelled', due_time=NULL, updated_time=?
            WHERE id=?
            """,
            (
                (REMIND_AT + timedelta(seconds=1)).isoformat(),
                (REMIND_AT + timedelta(seconds=1)).isoformat(),
                reminder_id,
            ),
        )
    run_email(context, REMIND_AT + timedelta(seconds=2))
    assert email_rows(context)[0]["status"] == "suppressed"
    assert context["sender"].reminder_calls == []


def test_address_change_suppresses_old_snapshot_without_redirect(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context, address="old@example.com")
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    context["emails"].set_candidate_address(
        context["user"].id,
        "new@example.com",
        changed_at=REMIND_AT + timedelta(seconds=1),
    )
    run_email(context, REMIND_AT + timedelta(seconds=2))
    assert email_rows(context)[0]["status"] == "suppressed"
    assert context["sender"].reminder_calls == []


def test_disabled_or_paused_after_queue_suppresses_without_provider_call(
    delivery_context,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    context["emails"].set_enabled(
        context["user"].id, False, changed_at=REMIND_AT + timedelta(seconds=1)
    )
    run_email(context, REMIND_AT + timedelta(seconds=2))
    assert email_rows(context)[0]["status"] == "suppressed"
    assert context["sender"].reminder_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        """
        UPDATE email_reminder_settings
        SET verification_status='pending', verified_at=NULL
        """,
        """
        UPDATE email_reminder_settings
        SET health_status='paused', pause_reason='hard_rejected'
        """,
        "UPDATE users SET status='disabled'",
    ],
)
def test_pending_paused_or_disabled_state_is_rechecked_before_send(
    delivery_context,
    mutation,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    with context["database"].transaction() as connection:
        connection.execute(mutation)
    run_email(context, REMIND_AT + timedelta(seconds=2))
    assert email_rows(context)[0]["status"] == "suppressed"
    assert context["sender"].reminder_calls == []


def test_cold_start_sends_existing_queued_delivery_within_ttl(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)

    restarted_sender = RecordingEmailSender()
    restarted_service = EmailReminderService(
        EmailReminderRepository(context["database"]),
        restarted_sender,
        context["settings"],
    )
    result = restarted_service.run_sweep_once(
        now_utc=REMIND_AT + timedelta(minutes=5),
        batch_size=20,
        stale_after_seconds=300,
    )
    assert result.accepted == 1
    assert len(restarted_sender.reminder_calls) == 1
    assert email_rows(context)[0]["status"] == "accepted"


def test_permanently_deleted_item_cascades_delivery_without_send(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    context["items"].update_item(
        context["item_id"],
        context["user"].id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    context["items"].permanently_delete_item(
        context["item_id"], context["user"].id
    )
    run_email(context, REMIND_AT + timedelta(seconds=2))
    assert email_rows(context) == []
    assert context["sender"].reminder_calls == []


@pytest.mark.parametrize(
    ("deliver_status", "expected"),
    [(0, "pending"), (1, "delivered"), (2, "dropped"), (3, "rejected"), (8, "deferred")],
)
def test_provider_status_reconciliation(delivery_context, deliver_status, expected):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    context["sender"].queue_status_results(
        EmailStatusResult(
            EmailStatusQueryOutcome.FOUND,
            send_status=0,
            deliver_status=deliver_status,
            deliver_message="normalized status",
        )
    )

    run_email(context, REMIND_AT + timedelta(seconds=30))

    row = email_rows(context)[0]
    assert row["provider_delivery_status"] == expected
    assert row["status_check_count"] == 1
    if deliver_status in {0, 8}:
        assert row["next_status_check_at"] == (
            REMIND_AT + timedelta(minutes=5, seconds=30)
        ).isoformat()
    else:
        assert row["next_status_check_at"] is None


def test_complaint_pauses_only_matching_current_address(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context, address="old@example.com")
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    context["emails"].set_candidate_address(
        context["user"].id,
        "new@example.com",
        changed_at=REMIND_AT + timedelta(seconds=1),
    )
    context["sender"].queue_status_results(
        EmailStatusResult(
            EmailStatusQueryOutcome.FOUND,
            send_status=0,
            deliver_status=3,
            complained=True,
            pause_destination=True,
        )
    )
    run_email(context, REMIND_AT + timedelta(seconds=30))
    settings = context["emails"].get_settings(context["user"].id)
    assert settings.email_address == "new@example.com"
    assert settings.health_status.value == "healthy"


def test_status_query_failure_does_not_pause_address(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    context["sender"].queue_status_results(
        EmailStatusResult(
            EmailStatusQueryOutcome.FAILED,
            error_code="transport_error",
        )
    )
    run_email(context, REMIND_AT + timedelta(seconds=30))
    settings = context["emails"].get_settings(context["user"].id)
    assert settings.health_status.value == "healthy"
    assert email_rows(context)[0]["next_status_check_at"] is not None


def test_pending_status_reconciliation_stops_after_three_bounded_checks(
    delivery_context,
):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    pending = EmailStatusResult(
        EmailStatusQueryOutcome.FOUND,
        send_status=0,
        deliver_status=0,
    )
    context["sender"].queue_status_results(pending, pending, pending)
    for when in (
        REMIND_AT + timedelta(seconds=30),
        REMIND_AT + timedelta(minutes=5, seconds=30),
        REMIND_AT + timedelta(minutes=65, seconds=30),
    ):
        run_email(context, when)
    row = email_rows(context)[0]
    assert row["status_check_count"] == 3
    assert row["next_status_check_at"] is None
    run_email(context, REMIND_AT + timedelta(hours=2))
    assert len(context["sender"].status_calls) == 3


def test_complaint_pauses_matching_current_address(delivery_context):
    context = delivery_context
    schedule_reminder(context)
    make_email_eligible(context)
    context["reminders"].claim_due_reminders(as_of=REMIND_AT)
    run_email(context, REMIND_AT)
    context["sender"].queue_status_results(
        EmailStatusResult(
            EmailStatusQueryOutcome.FOUND,
            send_status=0,
            deliver_status=3,
            complained=True,
            pause_destination=True,
        )
    )
    run_email(context, REMIND_AT + timedelta(seconds=30))
    settings = context["emails"].get_settings(context["user"].id)
    assert settings.health_status.value == "paused"
    assert settings.pause_reason.value == "complaint"
