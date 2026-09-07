from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import Protocol, TypeVar

from app.services.alibaba_asr import AlibabaASRError, AlibabaASRResult
from app.services.voice_media import VoiceMediaError, VoiceMediaProcessor
from app.services.voice_storage import VoiceStorage
from app.voice_repository import VoiceCaptureRepository


class ASRProvider(Protocol):
    async def transcribe(
        self,
        audio_path: Path,
        *,
        format: str,
        sample_rate_hz: int | None,
    ) -> AlibabaASRResult: ...


MEDIA_FAILURE_MESSAGES = {
    "media_probe": "无法读取录音的实际媒体格式；原始录音已保留。",
    "unsupported_media": "录音格式或时长不受支持；原始录音已保留。",
    "conversion": "录音临时转码失败；原始录音已保留。",
}


PreparedAudio = TypeVar("PreparedAudio")


async def _finish_media_context(
    context: AbstractContextManager[PreparedAudio],
    exception_type=None,
    exception=None,
    traceback=None,
) -> bool | None:
    cleanup = asyncio.create_task(
        asyncio.to_thread(
            context.__exit__,
            exception_type,
            exception,
            traceback,
        )
    )
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        try:
            await cleanup
        finally:
            raise


@asynccontextmanager
async def _media_context_in_thread(
    context: AbstractContextManager[PreparedAudio],
) -> AsyncIterator[PreparedAudio]:
    enter = asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        prepared = await asyncio.shield(enter)
    except asyncio.CancelledError as cancellation:
        try:
            prepared = await enter
        except BaseException:
            pass
        else:
            await _finish_media_context(context)
        raise cancellation

    try:
        yield prepared
    except BaseException as exc:
        suppressed = await _finish_media_context(
            context,
            type(exc),
            exc,
            exc.__traceback__,
        )
        if not suppressed:
            raise
    else:
        await _finish_media_context(context)


class VoiceTranscriptionService:
    def __init__(
        self,
        repository: VoiceCaptureRepository,
        storage: VoiceStorage,
        media: VoiceMediaProcessor,
        provider: ASRProvider,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.media = media
        self.provider = provider

    async def process_initial(self, segment_id: int, user_id: int) -> None:
        if not self.repository.claim_pending_segment(segment_id, user_id):
            return
        try:
            segment = self.repository.get_segment_record(segment_id, user_id)
            original_path = self.storage.resolve_original(segment.storage_key)
            metadata = await asyncio.to_thread(self.media.probe, original_path)
            if not self.repository.set_media_metadata(segment_id, user_id, metadata):
                return
            media_context = self.media.prepare_asr_audio(original_path, metadata)
            async with _media_context_in_thread(media_context) as prepared:
                if not self.repository.set_asr_input_kind(
                    segment_id,
                    user_id,
                    prepared.input_kind,
                ):
                    return
                result = await self.provider.transcribe(
                    prepared.path,
                    format=prepared.format,
                    sample_rate_hz=prepared.sample_rate_hz,
                )
            self.repository.complete_transcription(
                segment_id,
                user_id,
                transcript=result.transcript,
                provider_request_id=result.request_id,
            )
        except asyncio.CancelledError:
            self.repository.mark_segment_failed(
                segment_id,
                user_id,
                failure_code="interrupted",
                failure_message="转写被服务中断；原始录音已保留，请手动重试。",
            )
            raise
        except VoiceMediaError as exc:
            self.repository.mark_segment_failed(
                segment_id,
                user_id,
                failure_code=exc.failure_code,
                failure_message=MEDIA_FAILURE_MESSAGES.get(
                    exc.failure_code,
                    "无法处理录音媒体；原始录音已保留。",
                ),
            )
        except AlibabaASRError as exc:
            self.repository.mark_segment_failed(
                segment_id,
                user_id,
                failure_code=exc.failure_code,
                failure_message=exc.user_message,
            )
        except Exception:
            self.repository.mark_segment_failed(
                segment_id,
                user_id,
                failure_code="internal",
                failure_message="语音转写出现内部错误；原始录音已保留。",
            )
