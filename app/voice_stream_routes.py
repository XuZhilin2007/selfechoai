"""Authenticated browser-to-provider streaming attempt and durable Draft acceptance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import InvalidCsrfTokenError, InvalidSessionError
from app.auth_routes import session_cookie_name
from app.provider_admission import ProviderAdmissionDenied, ProviderPermit
from app.services.alibaba_asr import AlibabaASRError
from app.services.alibaba_streaming_asr import AlibabaStreamingASRSession, StreamingTranscript
from app.services.storage_admission import (
    StorageAdmissionDenied, VoiceStorageLease,
)
from app.voice_repository import VoiceCaptureRepository


logger = logging.getLogger(__name__)
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PCM_BYTES = 2_000_000  # 60 seconds of mono 16 kHz PCM16 plus margin.
MAX_ACTIVE_VOICE_STREAMS = 8


@dataclass(slots=True)
class StreamingAttempt:
    user_id: int
    session_id: int
    draft_id: int
    revision: int
    client_segment_id: str
    active: bool = True
    stopped: bool = False
    segment_id: int | None = None
    storage_reservation: VoiceStorageLease | None = None
    upload_started: bool = False
    upload_completed: bool = False
    cancelled: bool = False
    disconnected: bool = False
    original_saved: asyncio.Event = field(default_factory=asyncio.Event)
    stop_received: asyncio.Event = field(default_factory=asyncio.Event)


def create_voice_stream_router(repository: VoiceCaptureRepository) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["voice-capture"])

    @router.websocket("/capture-draft/voice-stream")
    async def voice_stream(websocket: WebSocket) -> None:
        app = websocket.app
        settings = app.state.settings
        session_token = websocket.cookies.get(session_cookie_name(settings))
        if (
            app.state.voice_stream_session_factory is None
            or websocket.headers.get("origin") != settings.app_origin
            or not session_token
        ):
            await websocket.close(code=4403)
            return
        try:
            session = app.state.auth_service.validate_session(session_token, touch=False)
        except InvalidSessionError:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        send_lock = asyncio.Lock()

        async def emit(message: dict) -> None:
            async with send_lock:
                await websocket.send_json(message)

        attempt: StreamingAttempt | None = None
        provider: AlibabaStreamingASRSession | None = None
        provider_permit: ProviderPermit | None = None
        storage_reservation: VoiceStorageLease | None = None
        browser_task: asyncio.Task | None = None
        provider_task: asyncio.Task | None = None
        saved_task: asyncio.Task | None = None
        stop_task: asyncio.Task | None = None
        success = False
        ready_sent = False
        handoff_for_upload = False
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), 5)
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise ValueError("expected hello")
            app.state.auth_service.validate_csrf_token(session, hello.get("csrf"))
            client_id = hello.get("client_segment_id")
            draft_id = hello.get("draft_id")
            revision = hello.get("revision")
            if (
                not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id)
                or not isinstance(draft_id, int) or isinstance(draft_id, bool)
                or not isinstance(revision, int) or isinstance(revision, bool)
            ):
                raise ValueError("invalid attempt identity")
            draft = repository.get_draft(session.user.id)
            if draft is None or draft.id != draft_id or draft.revision != revision:
                raise ValueError("Capture Draft changed")
            if any(segment.transcription_status in ("pending", "transcribing", "failed", "transcribed")
                   for segment in draft.voice_segments):
                raise ValueError("Capture Draft has an unresolved Voice Segment")
            active = app.state.voice_stream_attempts
            if session.user.id in active:
                raise ValueError("another Voice attempt is active")
            if len(active) >= MAX_ACTIVE_VOICE_STREAMS:
                raise ValueError("Voice streaming is temporarily busy")
            try:
                storage_reservation = app.state.storage_admission.reserve_voice()
            except StorageAdmissionDenied as exc:
                await emit({"type": "failed", "reason":
                            "storage_capacity" if exc.reason == "capacity"
                            else "storage_pressure"})
                return
            try:
                provider_permit = app.state.provider_admission.asr.acquire()
            except ProviderAdmissionDenied:
                await emit({"type": "failed", "reason": "capacity"})
                return
            attempt = StreamingAttempt(session.user.id, session.session.id,
                                       draft_id, revision, client_id,
                                       storage_reservation=storage_reservation)
            active[session.user.id] = attempt
            provider = app.state.voice_stream_session_factory()
            await asyncio.wait_for(provider.open(), settings.voice_asr_timeout_seconds)
            await emit({"type": "ready", "session_id": session.session.id})
            ready_sent = True
            transcript = StreamingTranscript()
            total_pcm = 0

            async def browser_events() -> None:
                nonlocal total_pcm
                try:
                    while True:
                        event = await websocket.receive()
                        if event["type"] == "websocket.disconnect":
                            attempt.disconnected = True
                            raise WebSocketDisconnect()
                        pcm = event.get("bytes")
                        if pcm is not None:
                            if attempt.stopped or not pcm or len(pcm) > 64 * 1024:
                                raise ValueError("invalid audio frame")
                            total_pcm += len(pcm)
                            if total_pcm > MAX_PCM_BYTES:
                                raise ValueError("audio duration exceeded")
                            await provider.send_audio(pcm)
                            continue
                        command = json.loads(event.get("text") or "")
                        if command.get("type") == "stop" and not attempt.stopped:
                            attempt.stopped = True
                            attempt.stop_received.set()
                            await provider.finish()
                            await emit({"type": "finishing"})
                        elif command.get("type") == "cancel":
                            attempt.cancelled = True
                            attempt.active = False
                            raise WebSocketDisconnect()
                        else:
                            raise ValueError("invalid stream command")
                finally:
                    attempt.active = False

            async def provider_events() -> str:
                while True:
                    event = await provider.receive()
                    if isinstance(event, tuple):
                        _, sentence = event
                        preview = transcript.accept(sentence)
                        await emit({"type": "preview", "text": preview})
                    elif event == "task-finished":
                        if not attempt.stopped:
                            raise ValueError("task ended before Stop")
                        return transcript.complete

            browser_task = asyncio.create_task(browser_events())
            provider_task = asyncio.create_task(provider_events())
            saved_task = asyncio.create_task(attempt.original_saved.wait())
            stop_task = asyncio.create_task(attempt.stop_received.wait())
            deadline = time.monotonic() + 125
            finish_deadline: float | None = None
            final_text: str | None = None
            while True:
                if browser_task.done():
                    browser_task.result()
                    raise ValueError("browser stream ended")
                if final_text is not None and attempt.original_saved.is_set():
                    break
                remaining = deadline - time.monotonic()
                if attempt.stopped:
                    if finish_deadline is None:
                        finish_deadline = time.monotonic() + 35
                    remaining = min(remaining, finish_deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("Voice streaming attempt timed out")
                watched = {browser_task}
                if final_text is None:
                    watched.add(provider_task)
                if not attempt.original_saved.is_set():
                    watched.add(saved_task)
                if not attempt.stopped:
                    watched.add(stop_task)
                done, _ = await asyncio.wait(watched, timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError("Voice streaming attempt timed out")
                if browser_task in done:
                    browser_task.result()
                    raise ValueError("browser stream ended")
                if provider_task in done:
                    final_text = provider_task.result()
                if saved_task in done and attempt.segment_id is None:
                    raise ValueError("Original Audio was not recorded")
            current = app.state.auth_service.validate_session(session_token, touch=False)
            if current.session.id != session.session.id or current.user.id != attempt.user_id:
                raise InvalidSessionError("session changed")
            if active.get(attempt.user_id) is not attempt or not attempt.active:
                raise ValueError("attempt is no longer authoritative")
            segment = repository.mark_segment_transcribed(
                attempt.segment_id, attempt.user_id,
                transcript=final_text, provider_request_id=provider.task_id,
            )
            if segment is None:
                raise ValueError("Voice Segment could not be finalized")
            success = True
            try:
                accepted = repository.accept_transcribed_segment(
                    segment.id, attempt.user_id
                )
            except Exception:
                logger.exception("Streaming Voice Draft acceptance failed for segment %s", segment.id)
                await emit({"type": "complete", "segment_id": segment.id,
                            "acceptance_pending": True})
            else:
                await emit({"type": "complete", "segment_id": accepted.id})
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except (AlibabaASRError, TimeoutError, InvalidSessionError, InvalidCsrfTokenError,
                ValueError, json.JSONDecodeError) as exc:
            if (isinstance(exc, (AlibabaASRError, TimeoutError))
                    and ready_sent and attempt is not None):
                handoff_for_upload = True
            try:
                await emit({"type": "failed", "reason": type(exc).__name__})
            except Exception:
                pass
        except Exception:
            logger.exception("Voice streaming attempt failed")
            try:
                await emit({"type": "failed", "reason": "internal"})
            except Exception:
                pass
        finally:
            try:
                if attempt is not None:
                    attempt.active = False
                    if app.state.voice_stream_attempts.get(attempt.user_id) is attempt:
                        del app.state.voice_stream_attempts[attempt.user_id]
                    if (handoff_for_upload and not attempt.upload_started
                            and not attempt.cancelled and not attempt.disconnected
                            and not attempt.original_saved.is_set()
                            and storage_reservation is not None):
                        app.state.storage_admission.park_stream_upload(
                            (attempt.user_id, attempt.session_id, attempt.draft_id,
                             attempt.revision, attempt.client_segment_id),
                            storage_reservation,
                        )
                    if attempt.segment_id is not None and not success:
                        try:
                            repository.mark_segment_failed(
                                attempt.segment_id, attempt.user_id,
                                failure_code="interrupted",
                                failure_message="实时转写未完成；原始录音已保留，请手动重试。",
                            )
                        except Exception:
                            logger.exception("Voice Segment failure transition was interrupted")
                for task in (browser_task, provider_task, saved_task, stop_task):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(task for task in (browser_task, provider_task, saved_task, stop_task)
                                       if task is not None), return_exceptions=True)
                if provider is not None:
                    try:
                        await asyncio.wait_for(provider.close(), 5)
                    except Exception:
                        logger.warning("Voice provider connection cleanup did not complete")
            finally:
                if provider_permit is not None:
                    provider_permit.release()
                if storage_reservation is not None:
                    storage_reservation.release()
                try:
                    await websocket.close()
                except Exception:
                    pass

    return router
