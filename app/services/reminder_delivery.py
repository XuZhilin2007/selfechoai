from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.reminder_repository import ReminderRepository, utc_now
from app.schemas import ReminderDeliveryStatus, WebPushOutcome
from app.services.email_reminders import EmailReminderService, EmailSweepResult
from app.services.web_push import WebPushService, build_reminder_push_payload


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReminderSweepResult:
    claimed_reminders: int
    queued_deliveries: int
    attempted_deliveries: int
    sent_deliveries: int
    failed_deliveries: int
    stale_unknown_deliveries: int
    inactive_target_deliveries: int
    email_stale_unknown: int = 0
    email_attempted: int = 0
    email_accepted: int = 0
    email_retry_wait: int = 0
    email_failed: int = 0
    email_expired: int = 0
    email_suppressed: int = 0
    email_ambiguous: int = 0
    email_status_checks: int = 0


class ReminderDeliveryService:
    """Coordinate durable Reminder delivery with at-most-once attempts."""

    def __init__(
        self,
        repository: ReminderRepository,
        web_push_service: WebPushService,
        email_reminder_service: EmailReminderService | None = None,
        *,
        delivery_enabled: bool = True,
        email_delivery_enabled: bool = False,
    ) -> None:
        self.repository = repository
        self.web_push_service = web_push_service
        self.email_reminder_service = email_reminder_service
        self.push_delivery_enabled = delivery_enabled
        self.email_delivery_enabled = email_delivery_enabled

    def run_reminder_sweep_once(
        self,
        *,
        now_utc: datetime | None = None,
        batch_size: int,
        stale_after_seconds: int,
    ) -> ReminderSweepResult:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        sweep_time = now_utc or utc_now()

        stale_unknown = self.repository.reconcile_stale_sending_deliveries(
            as_of=sweep_time,
            stale_after_seconds=stale_after_seconds,
        )
        claim = self.repository.claim_due_reminders(
            as_of=sweep_time,
            batch_size=batch_size,
            queue_push_deliveries=self.push_delivery_enabled,
            queue_email_deliveries=self.email_delivery_enabled,
        )
        inactive_targets = self.repository.fail_unusable_queued_deliveries(
            as_of=sweep_time,
            batch_size=batch_size,
        )

        attempted = 0
        sent = 0
        failed = inactive_targets
        if self.push_delivery_enabled:
            for _ in range(batch_size):
                attempt_time = now_utc or utc_now()
                target = self.repository.claim_next_queued_delivery(
                    as_of=attempt_time
                )
                if target is None:
                    break
                attempted += 1
                try:
                    result = self.web_push_service.send(
                        target.subscription,
                        build_reminder_push_payload(),
                    )
                except Exception as exc:  # isolate target without logging secrets
                    logger.error(
                        "Reminder delivery transport raised unexpectedly "
                        "delivery_id=%s reminder_id=%s subscription_id=%s "
                        "exception_type=%s",
                        target.delivery.id,
                        target.delivery.reminder_id,
                        target.delivery.subscription_id,
                        type(exc).__name__,
                    )
                    terminal_status = ReminderDeliveryStatus.FAILED
                    provider_status = f"worker_{type(exc).__name__}"[:100]
                    invalidate_subscription = False
                else:
                    terminal_status = (
                        ReminderDeliveryStatus.SENT
                        if result.outcome == WebPushOutcome.ACCEPTED
                        else ReminderDeliveryStatus.FAILED
                    )
                    provider_status = (
                        str(result.provider_status)
                        if result.provider_status is not None
                        else result.error_code or result.outcome.value
                    )
                    invalidate_subscription = (
                        result.outcome == WebPushOutcome.SUBSCRIPTION_GONE
                    )
                    if terminal_status == ReminderDeliveryStatus.FAILED:
                        logger.warning(
                            "Reminder delivery failed delivery_id=%s "
                            "reminder_id=%s subscription_id=%s outcome=%s "
                            "provider_status=%s",
                            target.delivery.id,
                            target.delivery.reminder_id,
                            target.delivery.subscription_id,
                            result.outcome.value,
                            result.provider_status,
                        )

                try:
                    terminal = self.repository.finish_delivery(
                        target.delivery.id,
                        target.delivery.user_id,
                        status=terminal_status,
                        provider_status=provider_status,
                        finished_time=now_utc or utc_now(),
                        invalidate_subscription=invalidate_subscription,
                    )
                except Exception as exc:
                    # Durable sending remains ambiguous and later becomes unknown.
                    logger.error(
                        "Reminder delivery outcome could not be persisted "
                        "delivery_id=%s reminder_id=%s subscription_id=%s "
                        "exception_type=%s",
                        target.delivery.id,
                        target.delivery.reminder_id,
                        target.delivery.subscription_id,
                        type(exc).__name__,
                    )
                    continue
                if terminal.status == ReminderDeliveryStatus.SENT:
                    sent += 1
                elif terminal.status == ReminderDeliveryStatus.FAILED:
                    failed += 1

        email_result = (
            self.email_reminder_service.run_sweep_once(
                now_utc=now_utc,
                batch_size=batch_size,
                stale_after_seconds=stale_after_seconds,
            )
            if self.email_reminder_service is not None
            else EmailSweepResult()
        )
        return ReminderSweepResult(
            claimed_reminders=len(claim.claimed_reminder_ids),
            queued_deliveries=len(claim.queued_delivery_ids),
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            stale_unknown_deliveries=stale_unknown,
            inactive_target_deliveries=inactive_targets,
            email_stale_unknown=email_result.stale_unknown,
            email_attempted=email_result.attempted,
            email_accepted=email_result.accepted,
            email_retry_wait=email_result.retry_wait,
            email_failed=email_result.failed,
            email_expired=email_result.expired,
            email_suppressed=email_result.suppressed,
            email_ambiguous=email_result.ambiguous,
            email_status_checks=email_result.status_checks,
        )


async def run_reminder_polling_worker(
    service: ReminderDeliveryService,
    settings: Settings,
) -> None:
    """Run an immediate sweep followed by bounded-interval polling."""

    while True:
        try:
            result = await asyncio.to_thread(
                service.run_reminder_sweep_once,
                batch_size=settings.reminder_batch_size,
                stale_after_seconds=settings.reminder_sending_stale_seconds,
            )
            if (
                result.claimed_reminders
                or result.attempted_deliveries
                or result.stale_unknown_deliveries
                or result.inactive_target_deliveries
                or result.email_stale_unknown
                or result.email_attempted
                or result.email_status_checks
            ):
                logger.info(
                    "Reminder sweep completed claimed=%s queued=%s "
                    "attempted=%s sent=%s failed=%s unknown=%s inactive=%s "
                    "email_attempted=%s email_accepted=%s email_retry=%s "
                    "email_failed=%s email_expired=%s email_suppressed=%s "
                    "email_unknown=%s email_status_checks=%s",
                    result.claimed_reminders,
                    result.queued_deliveries,
                    result.attempted_deliveries,
                    result.sent_deliveries,
                    result.failed_deliveries,
                    result.stale_unknown_deliveries,
                    result.inactive_target_deliveries,
                    result.email_attempted,
                    result.email_accepted,
                    result.email_retry_wait,
                    result.email_failed,
                    result.email_expired,
                    result.email_suppressed,
                    result.email_ambiguous,
                    result.email_status_checks,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Reminder sweep failed and will continue next interval "
                "exception_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(settings.reminder_poll_interval_seconds)
