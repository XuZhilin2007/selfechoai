from __future__ import annotations

import logging

from app.repository import Repository
from app.schemas import FailureType, PersonalItemPublic
from app.services.ai import AIService, AIServiceError


logger = logging.getLogger(__name__)


class InputProcessingService:
    def __init__(self, repository: Repository, ai_service: AIService) -> None:
        self.repository = repository
        self.ai_service = ai_service

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
            if existing_item is None:
                self.repository.create_item_from_input(
                    input_id,
                    user_id,
                    extraction.fields,
                    extraction.evidence_fields,
                )
            else:
                self.repository.apply_ai_update(
                    input_id,
                    existing_item.id,
                    user_id,
                    extraction.fields,
                    extraction.evidence_fields,
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
