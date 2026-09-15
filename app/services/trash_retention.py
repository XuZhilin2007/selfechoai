from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.repository import Repository
from app.services.voice_deletions import VoiceDeletionLedger


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrashRetentionSweepResult:
    purged_items: int
    voice_selected: int = 0
    voice_deleted_or_absent: int = 0
    voice_failed: int = 0


class TrashRetentionService:
    """Bounded DB-first Trash expiry with optional post-commit Voice cleanup."""

    def __init__(
        self,
        repository: Repository,
        voice_deletion_ledger: VoiceDeletionLedger | None = None,
    ) -> None:
        self.repository = repository
        self.voice_deletion_ledger = voice_deletion_ledger

    def run_sweep_once(
        self,
        *,
        now_utc: datetime | None = None,
        batch_size: int = 100,
    ) -> TrashRetentionSweepResult:
        purged_ids = self.repository.purge_expired_trash(
            now_utc=now_utc,
            batch_size=batch_size,
        )
        ledger = self.voice_deletion_ledger
        if ledger is None:
            return TrashRetentionSweepResult(purged_items=len(purged_ids))

        # This runs only after purge_expired_trash commits. Any failed physical
        # deletion therefore remains durable and retryable in the ledger.
        voice_result = ledger.drain(limit=max(25, batch_size))
        return TrashRetentionSweepResult(
            purged_items=len(purged_ids),
            voice_selected=voice_result.selected,
            voice_deleted_or_absent=voice_result.deleted_or_absent,
            voice_failed=voice_result.failed,
        )


async def run_trash_retention_worker(
    service: TrashRetentionService,
    settings: Settings,
) -> None:
    """Run an immediate sweep and keep polling after isolated failures."""

    while True:
        try:
            result = await asyncio.to_thread(
                service.run_sweep_once,
                batch_size=settings.trash_retention_batch_size,
            )
            if result.purged_items or result.voice_selected:
                logger.info(
                    "Trash retention sweep completed purged=%s "
                    "voice_selected=%s voice_deleted=%s voice_failed=%s",
                    result.purged_items,
                    result.voice_selected,
                    result.voice_deleted_or_absent,
                    result.voice_failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Trash retention sweep failed and will continue next interval "
                "exception_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(settings.trash_retention_poll_interval_seconds)
