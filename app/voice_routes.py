from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path as PathParameter,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from app.repository import NotFoundError
from app.schemas import (
    CaptureDraftPublic,
    CaptureDraftPutRequest,
    CaptureDraftResponse,
    CaptureDraftSaveRequest,
    ItemInputPublic,
    ProcessingStatus,
    UserPublic,
    VoiceSegmentPublic,
)
from app.services.storage_admission import (
    StorageAdmissionDenied,
    VoiceStorageLease,
)
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_media import content_type_for_container
from app.services.voice_storage import (
    EmptyVoiceUploadError,
    StoredOriginal,
    VoiceStorage,
    VoiceStorageCapacityError,
    VoiceStorageError,
    VoiceUploadTooLargeError,
)
from app.voice_repository import (
    CaptureDraftRecord,
    DraftBlockedError,
    DraftRevisionConflictError,
    DraftTextLimitError,
    VoiceCaptureRepository,
    VoiceSegmentConflictError,
    VoiceSegmentRecord,
)
from app.voice_runtime import VoiceRuntime


logger = logging.getLogger(__name__)


def _database_storage_admission_response(
    exc: StorageAdmissionDenied,
) -> HTTPException:
    if exc.reason == "capacity":
        return HTTPException(
            status_code=507, detail="存储空间暂时不足；内容尚未保存，请稍后重试。",
        )
    return HTTPException(
        status_code=429, detail="保存操作暂时过于频繁；内容尚未保存，请稍后重试。",
    )


def _voice_storage_admission_response(
    exc: StorageAdmissionDenied,
) -> HTTPException:
    if exc.reason == "capacity":
        return HTTPException(
            status_code=507,
            detail="存储空间暂时不足；原始录音尚未保存，请稍后重试。",
        )
    return HTTPException(
        status_code=429,
        detail="录音保存暂时过于频繁；原始录音尚未保存，请稍后重试。",
    )


@contextmanager
def _database_growth(request: Request, estimated_growth_bytes: int = 0):
    try:
        lease = request.app.state.storage_admission.reserve_database_growth(
            estimated_growth_bytes
        )
    except StorageAdmissionDenied as exc:
        raise _database_storage_admission_response(exc) from exc
    with lease:
        yield


def create_voice_router(
    repository: VoiceCaptureRepository,
    require_current_user: Callable[..., UserPublic],
    require_csrf_current_user: Callable[..., UserPublic],
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["voice-capture"])

    @router.get("/capture-draft", response_model=CaptureDraftResponse)
    def get_capture_draft(
        request: Request,
        current_user: UserPublic = Depends(require_current_user),
    ) -> CaptureDraftResponse:
        return CaptureDraftResponse(
            draft=_public_draft(repository.get_draft(current_user.id)),
            voice_available=_voice_available(request),
        )

    @router.put("/capture-draft", response_model=CaptureDraftResponse)
    def put_capture_draft(
        payload: CaptureDraftPutRequest,
        request: Request,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> CaptureDraftResponse:
        existing = repository.get_draft(current_user.id)
        new_bytes = len(payload.current_text.encode("utf-8"))
        old_bytes = len(existing.current_text.encode("utf-8")) if existing else 0
        growth = max(0, new_bytes - old_bytes)
        with (_database_growth(request, growth) if existing is None or growth
              else nullcontext()):
            try:
                draft = repository.put_draft(
                    current_user.id,
                    payload.current_text,
                    payload.revision,
                )
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (DraftRevisionConflictError, DraftBlockedError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except DraftTextLimitError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CaptureDraftResponse(
            draft=_public_draft(draft),
            voice_available=_voice_available(request),
        )

    @router.post(
        "/capture-draft/save",
        response_model=ItemInputPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def save_capture_draft(
        payload: CaptureDraftSaveRequest,
        request: Request,
        background_tasks: BackgroundTasks,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ItemInputPublic:
        try:
            with _database_growth(request):
                result = repository.save_draft(
                    current_user.id,
                    payload.draft_id,
                    payload.revision,
                )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (DraftRevisionConflictError, DraftBlockedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.item_input.processing_status == ProcessingStatus.PENDING:
            background_tasks.add_task(
                request.app.state.processor.process_input,
                result.item_input.id,
                current_user.id,
            )
        return result.item_input

    @router.put(
        "/capture-draft/voice-segments/{client_segment_id}",
        response_model=VoiceSegmentPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_voice_segment(
        client_segment_id: Annotated[
            str,
            PathParameter(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
            ),
        ],
        request: Request,
        revision: int = Query(ge=0),
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> VoiceSegmentPublic:
        storage, ledger, runtime = _require_voice_services(request)
        saved, lease = await _store_uploaded_original(
            request, repository, storage,
        )
        client_content_type = request.headers.get("content-type")
        if client_content_type is not None:
            client_content_type = client_content_type[:200]
        try:
            result = repository.create_pending_segment(
                user_id=current_user.id,
                client_segment_id=client_segment_id,
                saved=saved,
                client_content_type=client_content_type,
                expected_revision=revision,
            )
        except NotFoundError as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            DraftRevisionConflictError,
            DraftBlockedError,
            VoiceSegmentConflictError,
        ) as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            await _drain_voice_deletions_async(request)
            logger.error(
                "Voice Segment registration failed safely: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Voice Segment could not be registered safely",
            ) from exc
        finally:
            lease.release()

        if not result.created:
            await _drain_voice_deletions_async(request)
        if result.segment.transcription_status == "pending":
            _schedule_voice(runtime, result.segment.id, current_user.id)
        return _public_segment(result.segment)

    @router.post(
        "/voice-segments/{segment_id}/accept",
        response_model=VoiceSegmentPublic,
    )
    def accept_streaming_voice_segment(
        segment_id: int,
        request: Request,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> VoiceSegmentPublic:
        try:
            with _database_growth(request):
                segment = repository.accept_transcribed_segment(
                    segment_id,
                    current_user.id,
                )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (DraftTextLimitError, VoiceSegmentConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _public_segment(segment)

    @router.post(
        "/voice-segments/{segment_id}/retry",
        response_model=VoiceSegmentPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_voice_segment(
        segment_id: int,
        request: Request,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> VoiceSegmentPublic:
        try:
            existing = repository.get_segment(segment_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime = request.app.state.voice_runtime
        if existing.failure_code != "draft_text_limit" and runtime is None:
            raise HTTPException(status_code=503, detail="Voice ASR is disabled")
        try:
            with _database_growth(request):
                segment, needs_processing = repository.retry_failed_segment(
                    segment_id,
                    current_user.id,
                )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (DraftBlockedError, DraftTextLimitError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if needs_processing:
            _schedule_voice(runtime, segment_id, current_user.id)
        return _public_segment(segment)

    @router.delete(
        "/voice-segments/{segment_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_voice_segment(
        segment_id: int,
        request: Request,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> Response:
        try:
            repository.delete_failed_segment(segment_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DraftBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        drain_voice_deletions(request)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/voice-segments/{segment_id}/audio")
    def get_voice_segment_audio(
        segment_id: int,
        request: Request,
        current_user: UserPublic = Depends(require_current_user),
    ) -> Response:
        try:
            record = repository.get_segment_record(segment_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        storage = request.app.state.voice_storage
        if storage is None:
            raise HTTPException(status_code=503, detail="Voice storage is disabled")
        try:
            audio_path = storage.resolve_original(record.storage_key)
            if not audio_path.is_file():
                raise FileNotFoundError
            size = audio_path.stat().st_size
        except (OSError, VoiceStorageError) as exc:
            raise HTTPException(
                status_code=404,
                detail="Original Audio not found",
            ) from exc

        media_type = content_type_for_container(record.detected_container)
        base_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        }
        range_header = request.headers.get("range")
        if range_header is None:
            return StreamingResponse(
                _file_range(audio_path, 0, size - 1),
                status_code=200,
                media_type=media_type,
                headers={**base_headers, "Content-Length": str(size)},
            )
        parsed = _parse_single_range(range_header, size)
        if parsed is None:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes */{size}",
                    "Content-Type": media_type,
                },
            )
        start, end = parsed
        return StreamingResponse(
            _file_range(audio_path, start, end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers={
                **base_headers,
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{size}",
            },
        )

    @router.delete(
        "/capture-draft",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_capture_draft(
        request: Request,
        revision: int = Query(ge=0),
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> Response:
        try:
            repository.delete_draft(current_user.id, revision)
        except DraftRevisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        drain_voice_deletions(request)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _voice_available(request: Request) -> bool:
    return request.app.state.voice_runtime is not None


async def _store_uploaded_original(
    request: Request,
    repository: VoiceCaptureRepository,
    storage: VoiceStorage,
) -> tuple[StoredOriginal, VoiceStorageLease]:
    """Admit before the orphan ledger grows, then keep capacity through upload."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared > storage.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Voice upload is too large")
    try:
        lease = storage.reserve_original()
    except StorageAdmissionDenied as exc:
        raise _voice_storage_admission_response(exc) from exc
    complete = False
    try:
        storage_key = storage.allocate_original_key()
        with repository.database.transaction() as connection:
            VoiceDeletionLedger.record(connection, [storage_key], "orphan_cleanup")
        try:
            saved = await storage.store_original_async(
                request.stream(),
                storage_key=storage_key,
                reservation=lease,
            )
        except VoiceUploadTooLargeError as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(status_code=413, detail="Voice upload is too large") from exc
        except EmptyVoiceUploadError as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(status_code=400, detail="Voice upload body is empty") from exc
        except VoiceStorageCapacityError as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(
                status_code=507,
                detail="存储空间暂时不足；原始录音尚未保存，请稍后重试。",
            ) from exc
        except VoiceStorageError as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(
                status_code=500, detail="Original Audio could not be stored safely"
            ) from exc
        except Exception as exc:
            await _drain_voice_deletions_async(request)
            raise HTTPException(status_code=400, detail="Voice upload was interrupted") from exc
        complete = True
        return saved, lease
    finally:
        if not complete and lease is not None:
            lease.release()


def _public_segment(record: VoiceSegmentRecord) -> VoiceSegmentPublic:
    return VoiceSegmentPublic(
        id=record.id,
        position=record.position,
        client_segment_id=record.client_segment_id,
        original_size_bytes=record.original_size_bytes,
        client_content_type=record.client_content_type,
        detected_container=record.detected_container,
        detected_codec=record.detected_codec,
        sample_rate_hz=record.sample_rate_hz,
        channels=record.channels,
        duration_ms=record.duration_ms,
        transcription_status=record.transcription_status,
        provider_transcript=record.provider_transcript,
        failure_code=record.failure_code,
        failure_message=record.failure_message,
        attempt_count=record.attempt_count,
        created_time=record.created_time,
        updated_time=record.updated_time,
    )


def _public_draft(record: CaptureDraftRecord | None) -> CaptureDraftPublic | None:
    if record is None:
        return None
    return CaptureDraftPublic(
        id=record.id,
        current_text=record.current_text,
        revision=record.revision,
        created_time=record.created_time,
        updated_time=record.updated_time,
        voice_segments=[_public_segment(segment) for segment in record.voice_segments],
    )


def _require_voice_services(
    request: Request,
) -> tuple[VoiceStorage, VoiceDeletionLedger, VoiceRuntime]:
    storage = request.app.state.voice_storage
    ledger = request.app.state.voice_deletion_ledger
    runtime = request.app.state.voice_runtime
    if storage is None or ledger is None or runtime is None:
        raise HTTPException(status_code=503, detail="Voice ASR is disabled")
    return storage, ledger, runtime


def _schedule_voice(
    runtime: VoiceRuntime | None,
    segment_id: int,
    user_id: int,
) -> None:
    if runtime is None:
        return
    try:
        runtime.schedule(segment_id, user_id)
    except Exception as exc:
        logger.error(
            "Voice runtime scheduling failed after durable state: %s",
            type(exc).__name__,
        )


async def _drain_voice_deletions_async(request: Request) -> None:
    ledger = request.app.state.voice_deletion_ledger
    if ledger is None:
        return
    try:
        await asyncio.to_thread(ledger.drain, limit=25)
    except Exception as exc:
        logger.warning(
            "Voice deletion drain failed after durable ledger state: %s",
            type(exc).__name__,
        )


def drain_voice_deletions(request: Request) -> None:
    ledger = request.app.state.voice_deletion_ledger
    if ledger is None:
        return
    try:
        ledger.drain(limit=25)
    except Exception as exc:
        logger.warning(
            "Voice deletion drain failed after logical deletion: %s",
            type(exc).__name__,
        )


def _parse_single_range(value: str, size: int) -> tuple[int, int] | None:
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        return None
    specification = value[6:].strip()
    if "-" not in specification:
        return None
    start_text, end_text = specification.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(size - suffix_length, 0)
            return start, size - 1
        start = int(start_text)
        if start < 0 or start >= size:
            return None
        end = size - 1 if not end_text else int(end_text)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def _file_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as audio_file:
        audio_file.seek(start)
        while remaining > 0:
            chunk = audio_file.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
