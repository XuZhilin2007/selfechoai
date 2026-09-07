from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import AuthenticationService
from app.auth_repository import AuthRepository
from app.auth_routes import (
    create_auth_router,
    create_current_user_dependency,
    create_validated_session_dependency,
)
from app.config import Settings
from app.database import Database
from app.priority import rank_items
from app.push_routes import create_push_router
from app.reminder_repository import ReminderRepository
from app.reminder_routes import create_reminder_router
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.schemas import (
    CaptureRequest,
    DashboardItem,
    DashboardResponse,
    ItemDetailResponse,
    ItemInputPublic,
    ItemStatus,
    MessageResponse,
    PersonalItemPublic,
    ProcessingStatus,
    UserPublic,
    UserItemPatch,
)
from app.services.ai import AIService, AIServiceError, create_ai_service
from app.services.alibaba_asr import AlibabaASRClient
from app.services.processing import InputProcessingService
from app.services.push_security import PushEndpointPolicy
from app.services.push_subscriptions import PushSubscriptionService
from app.services.reminder_delivery import (
    ReminderDeliveryService,
    run_reminder_polling_worker,
)
from app.services.reminders import ReminderService
from app.services.web_push import WebPushService
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_media import VoiceMediaProcessor
from app.services.voice_storage import VoiceStorage
from app.services.voice_transcription import ASRProvider, VoiceTranscriptionService
from app.voice_repository import VoiceCaptureRepository
from app.voice_runtime import VoiceRuntime, validate_voice_runtime


def create_app(
    settings: Settings | None = None,
    ai_service: AIService | None = None,
    push_endpoint_policy: PushEndpointPolicy | None = None,
    voice_asr_provider: ASRProvider | None = None,
    voice_media_processor: VoiceMediaProcessor | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    database = Database(settings.database_path)
    repository = Repository(database)
    voice_repository = VoiceCaptureRepository(database)
    auth_repository = AuthRepository(database)
    reminder_repository = ReminderRepository(database)
    reminder_service = ReminderService(
        reminder_repository,
        repository,
        auth_repository,
    )
    push_endpoint_policy = push_endpoint_policy or PushEndpointPolicy()
    web_push_service = WebPushService(settings, push_endpoint_policy)
    push_subscription_service = PushSubscriptionService(
        reminder_repository,
        settings,
        web_push_service,
        push_endpoint_policy,
    )
    reminder_delivery_service = ReminderDeliveryService(
        reminder_repository,
        web_push_service,
        delivery_enabled=settings.web_push_enabled,
    )
    auth_service = AuthenticationService(
        auth_repository,
        registration_mode=settings.registration_mode,
        invite_code_hash=settings.invite_code_hash,
        session_expiration_seconds=settings.session_expiration_seconds,
    )
    ai_service = ai_service or create_ai_service(settings)
    processor = InputProcessingService(
        repository,
        ai_service,
        auth_repository=auth_repository,
        reminder_repository=reminder_repository,
    )
    recovery_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        voice_paths = validate_voice_runtime(settings)
        database.initialize()
        voice_runtime: VoiceRuntime | None = None
        if voice_paths is None:
            voice_repository.system_recover_interrupted_segments()
        else:
            voice_storage = VoiceStorage(
                voice_paths.storage_root,
                settings.voice_max_upload_bytes,
            )
            voice_deletion_ledger = VoiceDeletionLedger(database, voice_storage)
            media_processor = voice_media_processor or VoiceMediaProcessor(
                ffprobe_path=voice_paths.ffprobe_path,
                ffmpeg_path=voice_paths.ffmpeg_path,
                tmp_root=voice_storage.tmp_root,
            )
            asr_provider = voice_asr_provider or AlibabaASRClient(
                api_url=settings.alibaba_asr_api_url,
                api_key=settings.alibaba_api_key.get_secret_value(),
                timeout_seconds=settings.voice_asr_timeout_seconds,
            )
            voice_transcription_service = VoiceTranscriptionService(
                voice_repository,
                voice_storage,
                media_processor,
                asr_provider,
            )
            voice_runtime = VoiceRuntime(
                voice_repository,
                voice_transcription_service,
                voice_deletion_ledger,
            )
            application.state.voice_storage = voice_storage
            application.state.voice_deletion_ledger = voice_deletion_ledger
            application.state.voice_transcription_service = (
                voice_transcription_service
            )
            application.state.voice_runtime = voice_runtime
            await voice_runtime.start()
        repository.system_recover_interrupted_inputs()
        for pending_input in repository.system_list_pending_inputs():
            task = asyncio.create_task(
                processor.process_input(
                    pending_input.input_id,
                    pending_input.user_id,
                )
            )
            recovery_tasks.add(task)
            task.add_done_callback(recovery_tasks.discard)
        reminder_worker_task: asyncio.Task[None] | None = None
        if settings.reminder_worker_enabled:
            reminder_worker_task = asyncio.create_task(
                run_reminder_polling_worker(
                    reminder_delivery_service,
                    settings,
                )
            )
        application.state.reminder_worker_task = reminder_worker_task
        try:
            yield
        finally:
            if voice_runtime is not None:
                await voice_runtime.stop()
            if reminder_worker_task is not None:
                reminder_worker_task.cancel()
                await asyncio.gather(
                    reminder_worker_task,
                    return_exceptions=True,
                )
            for task in list(recovery_tasks):
                task.cancel()
            if recovery_tasks:
                await asyncio.gather(*recovery_tasks, return_exceptions=True)
            web_push_service.requests_session.close()

    app = FastAPI(
        title="SelfEcho AI",
        version="0.4.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.settings = settings
    app.state.repository = repository
    app.state.voice_repository = voice_repository
    app.state.voice_storage = None
    app.state.voice_deletion_ledger = None
    app.state.voice_transcription_service = None
    app.state.voice_runtime = None
    app.state.processor = processor
    app.state.auth_repository = auth_repository
    app.state.auth_service = auth_service
    app.state.reminder_repository = reminder_repository
    app.state.reminder_service = reminder_service
    app.state.push_endpoint_policy = push_endpoint_policy
    app.state.web_push_service = web_push_service
    app.state.push_subscription_service = push_subscription_service
    app.state.reminder_delivery_service = reminder_delivery_service
    app.include_router(create_auth_router(auth_service, settings))
    require_current_user = create_current_user_dependency(auth_service, settings)
    require_csrf_current_user = create_current_user_dependency(
        auth_service,
        settings,
        csrf_protected=True,
    )
    require_csrf_validated_session = create_validated_session_dependency(
        auth_service,
        settings,
        csrf_protected=True,
    )
    app.include_router(
        create_reminder_router(
            reminder_service,
            require_current_user,
            require_csrf_current_user,
        )
    )
    app.include_router(
        create_push_router(
            push_subscription_service,
            require_current_user,
            require_csrf_validated_session,
        )
    )

    def add_reminder_state(
        items: list[DashboardItem],
        user_id: int,
    ) -> list[DashboardItem]:
        enriched: list[DashboardItem] = []
        for item in items:
            reminder_state = reminder_service.get_item_state(item.id, user_id)
            enriched.append(
                item.model_copy(
                    update={
                        "reminder": reminder_state.reminder,
                        "show_reminder_prompt": reminder_state.show_reminder_prompt,
                    }
                )
            )
        return enriched

    @app.get("/api/health", response_model=MessageResponse)
    def health() -> MessageResponse:
        database.check_health()
        return MessageResponse(message="ok")

    @app.post(
        "/api/inputs",
        response_model=ItemInputPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def capture_input(
        payload: CaptureRequest,
        background_tasks: BackgroundTasks,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ItemInputPublic:
        item_input = repository.create_input(
            payload.original_text,
            payload.input_method,
            current_user.id,
        )
        background_tasks.add_task(
            processor.process_input,
            item_input.id,
            current_user.id,
        )
        return item_input

    @app.post(
        "/api/items/{item_id}/inputs",
        response_model=ItemInputPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def add_item_input(
        item_id: int,
        payload: CaptureRequest,
        background_tasks: BackgroundTasks,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ItemInputPublic:
        try:
            item_input = repository.create_input(
                payload.original_text,
                payload.input_method,
                current_user.id,
                item_id=item_id,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        background_tasks.add_task(
            processor.process_input,
            item_input.id,
            current_user.id,
        )
        return item_input

    @app.post(
        "/api/inputs/{input_id}/retry",
        response_model=ItemInputPublic,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_input(
        input_id: int,
        background_tasks: BackgroundTasks,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ItemInputPublic:
        try:
            item_input = repository.retry_input(input_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background_tasks.add_task(
            processor.process_input,
            item_input.id,
            current_user.id,
        )
        return item_input

    @app.post(
        "/api/items/{item_id}/reprocess",
        response_model=PersonalItemPublic,
    )
    async def reprocess_item(
        item_id: int,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> PersonalItemPublic:
        try:
            return await processor.reprocess_item(item_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AIServiceError as exc:
            raise HTTPException(status_code=502, detail=exc.user_message) from exc

    @app.get("/api/items", response_model=DashboardResponse)
    def dashboard(
        current_user: UserPublic = Depends(require_current_user),
        item_status: ItemStatus = Query(default=ItemStatus.ACTIVE, alias="status"),
    ) -> DashboardResponse:
        reminder_service.lazy_transition_due(current_user.id)
        sortable, needs_confirmation = rank_items(
            repository.list_items_by_status(item_status, current_user.id)
        )
        show_capture_queue = item_status == ItemStatus.ACTIVE
        return DashboardResponse(
            sortable_items=add_reminder_state(sortable, current_user.id),
            needs_confirmation=add_reminder_state(
                needs_confirmation,
                current_user.id,
            ),
            pending_inputs=(
                repository.list_unlinked_inputs(
                    [ProcessingStatus.PENDING, ProcessingStatus.PROCESSING],
                    current_user.id,
                )
                if show_capture_queue
                else []
            ),
            failed_inputs=(
                repository.list_unlinked_inputs(
                    [ProcessingStatus.FAILED], current_user.id
                )
                if show_capture_queue
                else []
            ),
            due_reminders=(
                reminder_service.list_due(
                    current_user.id,
                    unsurfaced_only=True,
                )
                if show_capture_queue
                else []
            ),
        )

    @app.get("/api/items/{item_id}", response_model=ItemDetailResponse)
    def item_detail(
        item_id: int,
        current_user: UserPublic = Depends(require_current_user),
    ) -> ItemDetailResponse:
        reminder_service.lazy_transition_due(current_user.id)
        try:
            item = repository.get_item(item_id, current_user.id)
            reminder_state = reminder_service.get_item_state(
                item_id,
                current_user.id,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ItemDetailResponse(
            item=item,
            inputs=repository.list_inputs_for_item(item_id, current_user.id),
            reminder=reminder_state.reminder,
            show_reminder_prompt=reminder_state.show_reminder_prompt,
        )

    @app.patch("/api/items/{item_id}", response_model=PersonalItemPublic)
    def update_item(
        item_id: int,
        payload: UserItemPatch,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> PersonalItemPublic:
        try:
            return repository.update_item(item_id, current_user.id, payload)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    def permanently_delete_item(
        item_id: int,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> Response:
        try:
            repository.permanently_delete_item(item_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    static_directory = Path(__file__).parent / "static"
    index_file = static_directory / "index.html"
    service_worker_file = static_directory / "service-worker.js"

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # Serving the worker from the application root gives it permission to
        # control all PWA routes without broad scope headers.
        return FileResponse(
            service_worker_file,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    @app.get("/register", include_in_schema=False)
    @app.get("/account", include_in_schema=False)
    @app.get("/capture", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/items/{item_id}", include_in_schema=False)
    def pwa_shell(item_id: int | None = None) -> FileResponse:
        return FileResponse(index_file)

    return app


app = create_app()
