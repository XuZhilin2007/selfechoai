from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from app.auth_repository import AuthRepository
from app.reminder_repository import ReminderRepository
from app.repository import NotFoundError, Repository, utc_now
from app.schemas import (
    AIReminderCandidate,
    FailureType,
    PersonalItemPublic,
    ReminderCreationCandidate,
)
from app.services.ai import AIService, AIServiceError
from app.services.temporal_parser import TemporalParser


logger = logging.getLogger(__name__)


class InputProcessingService:
    def __init__(
        self,
        repository: Repository,
        ai_service: AIService,
        *,
        auth_repository: AuthRepository | None = None,
        reminder_repository: ReminderRepository | None = None,
        temporal_parser: TemporalParser | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.ai_service = ai_service
        self.auth_repository = auth_repository or AuthRepository(repository.database)
        self.reminder_repository = reminder_repository or ReminderRepository(
            repository.database
        )
        self.temporal_parser = temporal_parser or TemporalParser()
        self.now_provider = now_provider or utc_now

    def _resolve_reminder(
        self,
        user_id: int,
        candidate: AIReminderCandidate,
    ) -> ReminderCreationCandidate | None:
        if not candidate.intent:
            return None
        user = self.auth_repository.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("user not found")
        result = self.temporal_parser.parse(
            candidate.temporal_expression,
            timezone_name=user.timezone,
            default_reminder_time=user.default_reminder_time,
            now_utc=self.now_provider(),
        )
        return ReminderCreationCandidate(
            source_expression=candidate.temporal_expression,
            scheduled_timezone=user.timezone,
            remind_at=result.remind_at,
        )

    async def process_input(self, input_id: int, user_id: int) -> None:
        if not self.repository.claim_input(input_id, user_id):
            return

        try:
            item_input = self.repository.get_input(input_id, user_id)
            existing_item = (
                self.repository.get_item(item_input.item_id, user_id)
                if item_input.item_id is not None
                else None
            )
            extraction = await self.ai_service.extract(
                item_input.original_text, existing_item
            )
            reminder = self._resolve_reminder(user_id, extraction.reminder)
            if existing_item is None:
                self.repository.create_item_from_input(
                    input_id,
                    user_id,
                    extraction.fields,
                    extraction.evidence_fields,
                    reminder=reminder,
                    reminder_repository=self.reminder_repository,
                )
            else:
                self.repository.apply_ai_update(
                    input_id,
                    existing_item.id,
                    user_id,
                    extraction.fields,
                    extraction.evidence_fields,
                    reminder=reminder,
                    reminder_repository=self.reminder_repository,
                )
        except AIServiceError as exc:
            # The raw input was already committed. Only its processing state changes.
            logger.error(
                "AI processing failed input_id=%s user_id=%s category=%s error=%s",
                input_id,
                user_id,
                exc.category.value,
                exc,
                exc_info=True,
            )
            self.repository.mark_input_failed(
                input_id, user_id, exc.category, exc.user_message
            )
        except Exception as exc:
            logger.exception(
                "Unexpected AI processing failure input_id=%s user_id=%s error_type=%s",
                input_id,
                user_id,
                type(exc).__name__,
            )
            self.repository.mark_input_failed(
                input_id,
                user_id,
                FailureType.INTERNAL,
                "AI 整理出现内部错误，原文已保存，请稍后重试。",
            )

    async def reprocess_item(
        self, item_id: int, user_id: int
    ) -> PersonalItemPublic:
        """Re-extract one item from its full history without creating new records."""

        existing_item = self.repository.get_item(item_id, user_id)
        inputs = self.repository.list_inputs_for_item(item_id, user_id)
        if not inputs:
            raise ValueError("item has no input history")
        history_text = "Reprocess full Item Input history (oldest to newest):\n" + (
            "\n".join(
                f"Item Input {index}: {item.original_text}"
                for index, item in enumerate(inputs, start=1)
            )
        )
        extraction = await self.ai_service.extract(history_text, existing_item)
        return self.repository.apply_reprocessed_fields(
            item_id,
            user_id,
            extraction.fields,
            extraction.evidence_fields,
        )
