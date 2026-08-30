from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.reminder_repository import ReminderRepository, utc_now
from app.schemas import ReminderDeliveryStatus, WebPushOutcome
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


class ReminderDeliveryService:
    """Coordinate durable Reminder delivery with at-most-once attempts."""

    def __init__(
        self,
        repository: ReminderRepository,
        web_push_service: WebPushService,
        *,
        delivery_enabled: bool = True,
    ) -> None:
        self.repository = repository
        self.web_push_service = web_push_service
        self.delivery_enabled = delivery_enabled

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
            queue_deliveries=self.delivery_enabled,
        )
        inactive_targets = self.repository.fail_unusable_queued_deliveries(
            as_of=sweep_time,
            batch_size=batch_size,
        )

        attempted = 0
        sent = 0
        failed = inactive_targets
        if self.delivery_enabled:
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

        return ReminderSweepResult(
            claimed_reminders=len(claim.claimed_reminder_ids),
            queued_deliveries=len(claim.queued_delivery_ids),
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            stale_unknown_deliveries=stale_unknown,
            inactive_target_deliveries=inactive_targets,
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
            ):
                logger.info(
                    "Reminder sweep completed claimed=%s queued=%s "
                    "attempted=%s sent=%s failed=%s unknown=%s inactive=%s",
                    result.claimed_reminders,
                    result.queued_deliveries,
                    result.attempted_deliveries,
                    result.sent_deliveries,
                    result.failed_deliveries,
                    result.stale_unknown_deliveries,
                    result.inactive_target_deliveries,
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
