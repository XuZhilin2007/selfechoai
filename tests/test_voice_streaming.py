from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pydantic import SecretStr

from app.auth_routes import CSRF_COOKIE_NAME
from app.auth import hash_invite_code
from app.config import Settings
from app.main import create_app
from app.services.ai import DisabledAIService
from app.services.alibaba_asr import AlibabaASRNetworkError, AlibabaASRResult
from app.services.alibaba_streaming_asr import AlibabaStreamingASRSession, StreamingTranscript
from app.services.voice_media import MediaMetadata, PreparedASRAudio
from app.services.voice_media import MediaProbeError
from app.services.storage_admission import DATABASE_WRITE_ALLOWANCE, StorageAdmissionDenied


class BatchProvider:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, path, *, format, sample_rate_hz):
        self.calls += 1
        return AlibabaASRResult("explicit retry", "batch-request")


class MediaProcessor:
    def probe(self, path):
        assert path.read_bytes() == b"complete-original-audio"
        return MediaMetadata("webm", "opus", 48000, 1, 850, "audio/webm")

    @contextmanager
    def prepare_asr_audio(self, path, metadata):
        yield PreparedASRAudio(path, "original_direct", "webm", None)


class StreamProvider:
    task_id = "synthetic-provider-task"

    def __init__(self) -> None:
        self.events = asyncio.Queue()
        self.closed = False
        self.fail = False
        self.delay_finish = 0

    async def open(self):
        pass

    async def send_audio(self, pcm):
        assert pcm
        await self.events.put(("result-generated", {
            "sentence_id": 1, "text": "first guess", "sentence_end": False,
        }))
        await self.events.put(("result-generated", {
            "sentence_id": 1, "text": "first revision", "sentence_end": False,
        }))

    async def finish(self):
        if self.delay_finish:
            await asyncio.sleep(self.delay_finish)
        if self.fail:
            await self.events.put(AlibabaASRNetworkError("synthetic disconnect"))
            return
        await self.events.put(("result-generated", {
            "sentence_id": 1, "text": "first sentence", "sentence_end": True,
        }))
        await self.events.put(("result-generated", {
            "sentence_id": 2, "text": "", "sentence_end": False,
        }))
        await self.events.put(("result-generated", {
            "sentence_id": 2, "text": "second sentence", "sentence_end": True,
        }))
        await self.events.put("task-finished")

    async def receive(self):
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return event

    async def close(self):
        self.closed = True


@pytest.fixture
def environment(tmp_path: Path):
    providers = []

    def provider_factory():
        provider = StreamProvider()
        providers.append(provider)
        return provider

    executable = Path(__import__("sys").executable).resolve()
    invite_code = "streaming-test-invite"
    settings = Settings(
        database_path=tmp_path / "app.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(invite_code),
        app_origin="http://testserver",
        session_cookie_secure=False,
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice").resolve(),
        voice_max_upload_bytes=1024,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_asr_api_url="https://workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
        alibaba_api_key=SecretStr("synthetic-key"),
    )
    app = create_app(settings=settings, ai_service=DisabledAIService(),
                     voice_asr_provider=BatchProvider(),
                     voice_media_processor=MediaProcessor(),
                     voice_stream_session_factory=provider_factory)
    with TestClient(app) as client:
        registration = client.post("/api/auth/register", json={
            "invite_code": invite_code,
            "email": "stream@example.com",
            "password": "synthetic password", "display_name": "Stream",
            "timezone": "Asia/Shanghai",
        })
        assert registration.status_code == 201
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
        draft = client.put("/api/capture-draft", json={"revision": 0,
                                                      "current_text": "original"}).json()["draft"]
        yield app, client, draft, providers


def _connect(client, draft, segment_id):
    return client.websocket_connect("/api/capture-draft/voice-stream",
                                    headers={"origin": "http://testserver"})


def _hello(ws, client, draft, segment_id):
    ws.send_json({"type": "hello", "csrf": client.cookies[CSRF_COOKIE_NAME],
                  "draft_id": draft["id"], "revision": draft["revision"],
                  "client_segment_id": segment_id})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return ready["session_id"]


def _upload(client, segment_id, draft, session_id, *, force_failed=False):
    return client.put(f"/api/capture-draft/streaming-voice-segments/{segment_id}",
                      params={"revision": draft["revision"],
                              "draft_id": draft["id"], "owner_id": 1,
                              "session_id": session_id,
                              "force_failed": force_failed},
                      content=b"complete-original-audio",
                      headers={"Content-Type": "audio/webm"})


def test_provider_finishes_before_original_then_accepts_persisted_transcript(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-one") as ws:
        session_id = _hello(ws, client, draft, "stream-one")
        ws.send_bytes(b"\x01\x00" * 320)
        assert ws.receive_json() == {"type": "preview", "text": "first guess"}
        assert ws.receive_json() == {"type": "preview", "text": "first revision"}
        ws.send_json({"type": "stop"})
        messages = [ws.receive_json() for _ in range(4)]
        assert any(item == {"type": "preview", "text": "first sentence second sentence"}
                   for item in messages)
        assert client.get("/api/capture-draft").json()["draft"]["voice_segments"] == []
        uploaded = _upload(client, "stream-one", draft, session_id)
        assert uploaded.status_code == 202
        assert uploaded.json()["transcription_status"] == "transcribing"
        assert ws.receive_json()["type"] == "complete"
        repeated = _upload(client, "stream-one", draft, session_id)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == uploaded.json()["id"]
    updated = client.get("/api/capture-draft").json()["draft"]
    assert updated["current_text"] == "original\nfirst sentence second sentence"
    assert updated["revision"] == draft["revision"] + 1
    assert updated["voice_segments"][0]["transcription_status"] == "succeeded"
    assert updated["voice_segments"][0]["attempt_count"] == 1
    assert updated["voice_segments"][0]["provider_transcript"] == "first sentence second sentence"
    assert updated["voice_segments"][0]["detected_container"] == "webm"
    assert client.get(f"/api/voice-segments/{uploaded.json()['id']}/audio").headers["content-type"].startswith("audio/webm")
    assert app.state.voice_repository.mark_segment_transcribed(
        updated["voice_segments"][0]["id"], 1, transcript="duplicate",
        provider_request_id="duplicate") is None
    assert providers[0].closed


def test_streaming_capacity_denial_precedes_provider_and_next_attempt_can_complete(environment):
    app, client, draft, providers = environment
    guard = app.state.provider_admission.asr
    assert app.state.voice_transcription_service.admission is guard
    occupied = [guard.acquire() for _ in range(guard.concurrent_limit)]
    with _connect(client, draft, "stream-denied") as ws:
        ws.send_json({"type": "hello", "csrf": client.cookies[CSRF_COOKIE_NAME],
                      "draft_id": draft["id"], "revision": draft["revision"],
                      "client_segment_id": "stream-denied"})
        assert ws.receive_json() == {"type": "failed", "reason": "capacity"}
    assert providers == []
    assert app.state.voice_stream_attempts == {}
    assert client.get("/api/capture-draft").json()["draft"]["current_text"] == "original"

    for permit in occupied:
        permit.release()
    with _connect(client, draft, "stream-admitted") as ws:
        session_id = _hello(ws, client, draft, "stream-admitted")
        ws.send_json({"type": "stop"})
        uploaded = _upload(client, "stream-admitted", draft, session_id)
        assert uploaded.status_code == 202
        while ws.receive_json()["type"] != "complete":
            pass
    assert providers[0].closed
    assert client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]["transcription_status"] == "succeeded"


def test_storage_low_water_rejects_before_streaming_provider_ready(
    environment, monkeypatch,
):
    app, client, draft, providers = environment
    guard = app.state.storage_admission
    free = {"value": guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE
            + guard.voice_max_bytes - 1}
    monkeypatch.setattr(guard, "_filesystem", lambda _path: (1, free["value"]))
    with _connect(client, draft, "storage-denied") as ws:
        ws.send_json({"type": "hello", "csrf": client.cookies[CSRF_COOKIE_NAME],
                      "draft_id": draft["id"], "revision": draft["revision"],
                      "client_segment_id": "storage-denied"})
        assert ws.receive_json() == {"type": "failed", "reason": "storage_capacity"}
    assert providers == []
    assert app.state.voice_stream_attempts == {}
    free["value"] += 1
    guard.voice_concurrent_limit = 1
    with _connect(client, draft, "storage-admitted") as ws:
        _hello(ws, client, draft, "storage-admitted")
        ws.send_json({"type": "cancel"})
    assert providers[0].closed
    with _connect(client, draft, "storage-after-cancel") as ws:
        _hello(ws, client, draft, "storage-after-cancel")
        ws.send_json({"type": "cancel"})
    assert providers[1].closed


def test_streaming_upload_uses_its_own_reservation_after_free_space_falls(
    environment, monkeypatch,
):
    app, client, draft, providers = environment
    guard = app.state.storage_admission
    free = {"value": guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE
            + guard.voice_max_bytes}
    monkeypatch.setattr(guard, "_filesystem", lambda _path: (1, free["value"]))
    with _connect(client, draft, "storage-held") as ws:
        session_id = _hello(ws, client, draft, "storage-held")
        free["value"] = guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE - 1
        with pytest.raises(StorageAdmissionDenied, match="capacity"):
            guard.reserve_voice()
        ws.send_json({"type": "stop"})
        uploaded = _upload(client, "storage-held", draft, session_id)
        assert uploaded.status_code == 202
        while ws.receive_json()["type"] != "complete":
            pass
    assert client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]["transcription_status"] == "succeeded"
    assert providers[0].closed


def test_failed_provider_hands_reserved_space_to_late_original_upload(
    environment, monkeypatch,
):
    app, client, draft, providers = environment
    guard = app.state.storage_admission
    free = {"value": guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE
            + guard.voice_max_bytes}
    monkeypatch.setattr(guard, "_filesystem", lambda _path: (1, free["value"]))
    with _connect(client, draft, "storage-handoff") as ws:
        session_id = _hello(ws, client, draft, "storage-handoff")
        providers[0].fail = True
        ws.send_json({"type": "stop"})
        while ws.receive_json()["type"] != "failed":
            pass
    free["value"] = guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE - 1
    uploaded = _upload(client, "storage-handoff", draft, session_id)
    assert uploaded.status_code == 202
    assert uploaded.json()["transcription_status"] == "failed"
    assert client.get(f"/api/voice-segments/{uploaded.json()['id']}/audio").status_code == 200
    free["value"] = 10**12
    with guard.reserve_voice():
        pass


def test_streaming_disconnect_and_provider_open_failure_release_storage_slot(environment):
    app, client, draft, providers = environment
    guard = app.state.storage_admission
    guard.voice_concurrent_limit = 1
    with _connect(client, draft, "storage-disconnect") as ws:
        _hello(ws, client, draft, "storage-disconnect")
        # Leaving the socket without Stop or Cancel is an abandoned gesture.
    assert app.state.voice_stream_attempts == {}

    class BrokenOpen(StreamProvider):
        async def open(self):
            raise AlibabaASRNetworkError("synthetic handshake failure")

    app.state.voice_stream_session_factory = BrokenOpen
    with _connect(client, draft, "storage-open-failed") as ws:
        ws.send_json({"type": "hello", "csrf": client.cookies[CSRF_COOKIE_NAME],
                      "draft_id": draft["id"], "revision": draft["revision"],
                      "client_segment_id": "storage-open-failed"})
        assert ws.receive_json()["type"] == "failed"
    assert app.state.voice_stream_attempts == {}
    app.state.voice_stream_session_factory = StreamProvider
    with _connect(client, draft, "storage-next") as ws:
        _hello(ws, client, draft, "storage-next")
        ws.send_json({"type": "cancel"})


def test_cancel_releases_streaming_provider_slot_for_next_attempt(environment):
    app, client, draft, providers = environment
    guard = app.state.provider_admission.asr
    occupied = [guard.acquire() for _ in range(guard.concurrent_limit - 1)]
    try:
        with _connect(client, draft, "stream-first-cancel") as ws:
            _hello(ws, client, draft, "stream-first-cancel")
            ws.send_json({"type": "cancel"})
        assert providers[0].closed
        with _connect(client, draft, "stream-second-cancel") as ws:
            _hello(ws, client, draft, "stream-second-cancel")
            ws.send_json({"type": "cancel"})
        assert providers[1].closed
        assert app.state.voice_stream_attempts == {}
    finally:
        for permit in occupied:
            permit.release()


def test_original_first_waits_for_whole_task(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-original-first") as ws:
        session_id = _hello(ws, client, draft, "stream-original-first")
        providers[0].delay_finish = 0.2
        ws.send_json({"type": "stop"})
        uploaded = _upload(client, "stream-original-first", draft, session_id)
        assert uploaded.status_code == 202
        assert uploaded.json()["transcription_status"] == "transcribing"
        assert client.get("/api/capture-draft").json()["draft"]["current_text"] == "original"
        while True:
            event = ws.receive_json()
            if event["type"] == "complete":
                break
        assert client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]["transcription_status"] == "succeeded"
    assert providers[0].closed


def _leave_transcribed(environment, monkeypatch, client_segment_id):
    app, client, draft, _ = environment
    repository = app.state.voice_repository
    accept = repository.accept_transcribed_segment
    def interrupted_accept(*_):
        raise RuntimeError("synthetic interruption")
    with monkeypatch.context() as patch:
        patch.setattr(repository, "accept_transcribed_segment", interrupted_accept)
        with _connect(client, draft, client_segment_id) as ws:
            session_id = _hello(ws, client, draft, client_segment_id)
            ws.send_json({"type": "stop"})
            uploaded = _upload(client, client_segment_id, draft, session_id)
            assert uploaded.status_code == 202
            while True:
                event = ws.receive_json()
                if event["type"] == "complete":
                    assert event["acceptance_pending"] is True
                    break
    assert repository.accept_transcribed_segment == accept
    segment_id = uploaded.json()["id"]
    assert client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]["transcription_status"] == "transcribed"
    return segment_id


def test_stranded_transcript_acceptance_is_idempotent_and_save_remains_explicit(environment, monkeypatch):
    app, client, draft, _ = environment
    segment_id = _leave_transcribed(environment, monkeypatch, "stream-stranded")
    assert client.post("/api/capture-draft/save", json={
        "draft_id": draft["id"], "revision": draft["revision"],
    }).status_code == 409
    assert client.post(f"/api/voice-segments/{segment_id}/accept").status_code == 200
    assert client.post(f"/api/voice-segments/{segment_id}/accept").status_code == 200
    assert client.post(f"/api/voice-segments/{segment_id}/retry").status_code == 409
    updated = client.get("/api/capture-draft").json()["draft"]
    assert updated["current_text"] == "original\nfirst sentence second sentence"
    assert updated["revision"] == draft["revision"] + 1
    assert updated["voice_segments"][0]["transcription_status"] == "succeeded"
    assert app.state.voice_transcription_service.provider.calls == 0
    with app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM item_inputs").fetchone()[0] == 0
    saved = client.post("/api/capture-draft/save", json={
        "draft_id": draft["id"], "revision": updated["revision"],
    })
    assert saved.status_code == 202
    assert saved.json()["original_text"] == updated["current_text"]


def test_concurrent_acceptance_and_transaction_rollback(environment, monkeypatch):
    app, client, draft, _ = environment
    segment_id = _leave_transcribed(environment, monkeypatch, "stream-concurrent")
    repository = app.state.voice_repository
    with app.state.database.transaction() as connection:
        connection.execute("""
            CREATE TRIGGER reject_voice_acceptance BEFORE UPDATE OF transcription_status
            ON voice_segments WHEN NEW.transcription_status = 'succeeded'
            BEGIN SELECT RAISE(FAIL, 'synthetic transaction failure'); END
        """)
    with pytest.raises(Exception, match="synthetic transaction failure"):
        repository.accept_transcribed_segment(segment_id, 1)
    unchanged = client.get("/api/capture-draft").json()["draft"]
    assert unchanged["current_text"] == "original"
    assert unchanged["revision"] == draft["revision"]
    assert unchanged["voice_segments"][0]["transcription_status"] == "transcribed"
    with app.state.database.transaction() as connection:
        connection.execute("DROP TRIGGER reject_voice_acceptance")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.accept_transcribed_segment(segment_id, 1), range(2)))
    assert all(result.transcription_status == "succeeded" for result in results)
    accepted = client.get("/api/capture-draft").json()["draft"]
    assert accepted["current_text"].count("first sentence second sentence") == 1
    assert accepted["revision"] == draft["revision"] + 1
    with pytest.raises(Exception, match="not found"):
        repository.accept_transcribed_segment(segment_id, 999)


def test_restart_recovery_accepts_persisted_result_without_asr(environment, monkeypatch):
    app, client, draft, _ = environment
    _leave_transcribed(environment, monkeypatch, "stream-recovery")
    batch = BatchProvider()
    restarted = create_app(settings=app.state.settings, ai_service=DisabledAIService(),
                           voice_asr_provider=batch, voice_media_processor=MediaProcessor(),
                           voice_stream_session_factory=StreamProvider)
    with TestClient(restarted):
        recovered = restarted.state.voice_repository.get_draft(1)
        assert recovered.current_text == "original\nfirst sentence second sentence"
        assert recovered.revision == draft["revision"] + 1
        assert recovered.voice_segments[0].transcription_status == "succeeded"
        assert batch.calls == 0
    assert app.state.voice_repository.accept_transcribed_segment(
        recovered.voice_segments[0].id, 1).transcription_status == "succeeded"


def test_text_limit_keeps_transcribed_until_draft_is_shortened(environment, monkeypatch):
    app, client, draft, _ = environment
    segment_id = _leave_transcribed(environment, monkeypatch, "stream-text-limit")
    long_text = "x" * 9990
    changed = client.put("/api/capture-draft", json={
        "revision": draft["revision"], "current_text": long_text,
    })
    assert changed.status_code == 200
    assert client.post(f"/api/voice-segments/{segment_id}/accept").status_code == 409
    blocked = client.get("/api/capture-draft").json()["draft"]
    assert blocked["current_text"] == long_text
    assert blocked["voice_segments"][0]["transcription_status"] == "transcribed"
    assert blocked["voice_segments"][0]["provider_transcript"] == "first sentence second sentence"
    assert client.put("/api/capture-draft", json={
        "revision": blocked["revision"], "current_text": "shortened",
    }).status_code == 200
    assert client.post(f"/api/voice-segments/{segment_id}/accept").status_code == 200
    assert client.get("/api/capture-draft").json()["draft"]["current_text"] == (
        "shortened\nfirst sentence second sentence"
    )
    assert app.state.voice_transcription_service.provider.calls == 0


def test_schema_rejects_missing_persisted_final_before_draft_acceptance(environment, monkeypatch):
    app, client, draft, _ = environment
    segment_id = _leave_transcribed(environment, monkeypatch, "stream-missing-final")
    with pytest.raises(Exception, match="CHECK constraint failed"):
        with app.state.database.transaction() as connection:
            connection.execute("UPDATE voice_segments SET provider_transcript = NULL WHERE id = ?",
                               (segment_id,))
    unchanged = client.get("/api/capture-draft").json()["draft"]
    assert unchanged["current_text"] == "original"
    assert unchanged["revision"] == draft["revision"]
    assert unchanged["voice_segments"][0]["transcription_status"] == "transcribed"
    assert unchanged["voice_segments"][0]["provider_transcript"] == "first sentence second sentence"


def test_partial_revision_empty_events_and_sentence_final_are_preview_only():
    transcript = StreamingTranscript()
    assert transcript.accept({}) == ""
    assert transcript.accept({"sentence_id": 1, "text": "guess", "sentence_end": False}) == "guess"
    assert transcript.accept({"sentence_id": 1, "text": "revision", "sentence_end": False}) == "revision"
    assert transcript.accept({"sentence_id": 1, "text": "", "sentence_end": False}) == "revision"
    assert transcript.accept({"sentence_id": 1, "text": "stable", "sentence_end": True}) == "stable"
    assert transcript.accept({"sentence_id": 2, "text": "next", "sentence_end": False}) == "stable next"
    assert transcript.accept({"sentence_id": 2, "text": "final", "sentence_end": True}) == "stable final"
    assert transcript.complete == "stable final"


def test_provider_adapter_uses_workspace_ws_duplex_protocol(monkeypatch):
    sent = []
    observed = {}

    class Socket:
        async def send(self, value):
            sent.append(value)

        async def recv(self):
            if not observed.get("started"):
                observed["started"] = True
                return json.dumps({"header": {"task_id": session.task_id,
                                               "event": "task-started"}})
            return json.dumps({"header": {"task_id": session.task_id,
                                           "event": "task-finished"}})

        async def close(self):
            observed["closed"] = True

    async def fake_connect(url, **kwargs):
        observed["url"] = url
        observed["headers"] = kwargs["additional_headers"]
        return Socket()

    monkeypatch.setattr("app.services.alibaba_streaming_asr.connect", fake_connect)
    session = AlibabaStreamingASRSession(
        "https://workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
        "synthetic-key", 2,
    )
    async def exercise():
        await session.open()
        await session.send_audio(b"\x01\x00")
        await session.finish()
        assert await session.receive() == "task-finished"
        await session.close()

    asyncio.run(exercise())
    assert observed["url"] == "wss://workspace.example.invalid/api-ws/v1/inference"
    assert observed["headers"] == {"Authorization": "Bearer synthetic-key"}
    assert json.loads(sent[0])["payload"]["model"] == "qwen-audio-3.0-asr-flash-streaming"
    assert json.loads(sent[0])["payload"]["parameters"] == {"format": "pcm", "sample_rate": 16000}
    assert sent[1] == b"\x01\x00"
    assert json.loads(sent[2])["header"]["action"] == "finish-task"
    assert observed["closed"]


def test_cancel_and_session_change_do_not_promote_or_cross_identity(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-cancel") as ws:
        session_id = _hello(ws, client, draft, "stream-cancel")
        ws.send_json({"type": "cancel"})
    assert app.state.voice_stream_attempts == {}
    assert client.get("/api/capture-draft").json()["draft"]["voice_segments"] == []
    assert providers[0].closed
    assert client.post("/api/auth/logout").status_code == 204
    login = client.post("/api/auth/login", json={
        "email": "stream@example.com", "password": "synthetic password",
    })
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
    assert _upload(client, "stream-cancel", draft, session_id).status_code == 403
    assert client.get("/api/capture-draft").json()["draft"]["voice_segments"] == []


def test_cancel_at_original_ready_boundary_cannot_promote_completed_provider(
    environment, monkeypatch,
):
    app, client, draft, providers = environment
    provider_finished = Event()
    original_ready = Event()
    allow_cancel = Event()
    original_wait = asyncio.wait

    async def wait_at_boundary(tasks, *, timeout=None, return_when=asyncio.FIRST_COMPLETED):
        done, pending = await original_wait(tasks, timeout=timeout,
                                            return_when=return_when)
        names = {task: task.get_coro().__qualname__ for task in tasks}
        if any(task in done and "provider_events" in name
               for task, name in names.items()):
            provider_finished.set()
        browser = next((task for task, name in names.items()
                        if "browser_events" in name), None)
        if (provider_finished.is_set() and browser is not None
                and any(task in done and name == "Event.wait"
                        for task, name in names.items())
                and not original_ready.is_set()):
            # Return the Original-ready snapshot only after Cancel has been
            # consumed by the browser task, recreating the missed-task window.
            original_ready.set()
            assert await asyncio.to_thread(allow_cancel.wait, 5)
            cancelled, _ = await original_wait({browser}, timeout=5)
            assert browser in cancelled
        return done, pending

    monkeypatch.setattr("app.voice_stream_routes.asyncio.wait", wait_at_boundary)
    with _connect(client, draft, "stream-cancel-boundary") as ws:
        session_id = _hello(ws, client, draft, "stream-cancel-boundary")
        ws.send_json({"type": "stop"})
        assert provider_finished.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            uploaded_future = pool.submit(
                _upload, client, "stream-cancel-boundary", draft, session_id,
            )
            try:
                assert original_ready.wait(5)
                ws.send_json({"type": "cancel"})
            finally:
                allow_cancel.set()
            uploaded = uploaded_future.result(timeout=5)
        assert uploaded.status_code == 202
    segment = client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]
    assert segment["transcription_status"] == "failed"
    assert segment["provider_transcript"] is None
    assert app.state.voice_stream_attempts == {}
    assert providers[0].closed


def test_stream_requires_origin_and_csrf_before_provider_is_started(environment):
    app, client, draft, providers = environment
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/api/capture-draft/voice-stream") as ws:
            ws.receive_json()
    assert rejected.value.code == 4403
    with _connect(client, draft, "stream-no-csrf") as ws:
        ws.send_json({"type": "hello", "csrf": "wrong",
                      "draft_id": draft["id"], "revision": draft["revision"],
                      "client_segment_id": "stream-no-csrf"})
        assert ws.receive_json()["type"] == "failed"
    assert providers == []


def test_disconnect_then_completed_original_is_failed_and_explicitly_retryable(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-failed") as ws:
        session_id = _hello(ws, client, draft, "stream-failed")
        providers[0].fail = True
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] in {"finishing", "failed"}
        # The browser still owns the complete MediaRecorder blob after provider failure.
    uploaded = _upload(client, "stream-failed", draft, session_id)
    assert uploaded.status_code == 202
    assert uploaded.json()["transcription_status"] == "failed"
    assert uploaded.json()["attempt_count"] == 1
    assert app.state.voice_transcription_service.provider.calls == 0
    retry = client.post(f"/api/voice-segments/{uploaded.json()['id']}/retry")
    assert retry.status_code == 202
    # Public schedules batch retries through VoiceRuntime; wait for the task.
    deadline = time.monotonic() + 2
    while True:
        completed = client.get("/api/capture-draft").json()["draft"]
        if completed["voice_segments"][0]["transcription_status"] == "succeeded":
            break
        assert time.monotonic() < deadline, "retry transcription did not finish"
        time.sleep(0.01)
    assert completed["voice_segments"][0]["attempt_count"] == 2
    assert completed["current_text"] == "original\nexplicit retry"
    assert app.state.voice_transcription_service.provider.calls == 1
    assert providers[0].closed


def test_provider_failure_after_original_save_cannot_promote_preview(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-late-fail") as ws:
        session_id = _hello(ws, client, draft, "stream-late-fail")
        providers[0].fail = True
        providers[0].delay_finish = 0.1
        ws.send_json({"type": "stop"})
        uploaded = _upload(client, "stream-late-fail", draft, session_id)
        assert uploaded.status_code == 202
        while True:
            event = ws.receive_json()
            if event["type"] == "failed":
                break
    segment = client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]
    assert segment["transcription_status"] == "failed"
    assert segment["provider_transcript"] is None
    assert client.get(f"/api/voice-segments/{segment['id']}/audio").content == b"complete-original-audio"
    assert app.state.voice_transcription_service.provider.calls == 0


def test_client_stream_failure_cannot_become_success_if_provider_finishes_late(environment):
    app, client, draft, providers = environment
    with _connect(client, draft, "stream-client-failed") as ws:
        session_id = _hello(ws, client, draft, "stream-client-failed")
        ws.send_json({"type": "stop"})
        for _ in range(4):
            ws.receive_json()
        uploaded = _upload(client, "stream-client-failed", draft, session_id,
                           force_failed=True)
        assert uploaded.status_code == 202
        assert uploaded.json()["transcription_status"] == "failed"
        assert ws.receive_json()["type"] == "failed"
    segment = client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]
    assert segment["transcription_status"] == "failed"
    assert segment["provider_transcript"] is None
    assert app.state.voice_transcription_service.provider.calls == 0


def test_invalid_original_is_failed_even_after_provider_completion(environment):
    app, client, draft, providers = environment

    class BrokenMedia:
        def probe(self, path):
            raise MediaProbeError("synthetic invalid media")

    app.state.voice_transcription_service.media = BrokenMedia()
    with _connect(client, draft, "stream-invalid-original") as ws:
        session_id = _hello(ws, client, draft, "stream-invalid-original")
        ws.send_json({"type": "stop"})
        for _ in range(4):
            ws.receive_json()
        uploaded = _upload(client, "stream-invalid-original", draft, session_id)
        assert uploaded.status_code == 202
        assert uploaded.json()["transcription_status"] == "failed"
        assert ws.receive_json()["type"] == "failed"
    segment = client.get("/api/capture-draft").json()["draft"]["voice_segments"][0]
    assert segment["provider_transcript"] is None
    assert segment["failure_code"] == "media_probe"


def test_streaming_routes_honor_public_secure_https_cookie_contract(tmp_path):
    """Streaming must read the __Host- session cookie used by Public HTTPS."""
    executable = Path(__import__("sys").executable).resolve()
    invite_code = "streaming-secure-invite"
    settings = Settings(
        database_path=tmp_path / "secure.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(invite_code),
        app_origin="http://testserver",
        session_cookie_secure=True,
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice").resolve(),
        voice_max_upload_bytes=1024,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_asr_api_url="https://workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
        alibaba_api_key=SecretStr("synthetic-key"),
    )
    app = create_app(settings=settings, ai_service=DisabledAIService(),
                     voice_asr_provider=BatchProvider(),
                     voice_media_processor=MediaProcessor(),
                     voice_stream_session_factory=StreamProvider)
    with TestClient(app) as client:
        registration = client.post("/api/auth/register", json={
            "invite_code": invite_code,
            "email": "secure@example.com",
            "password": "synthetic password", "display_name": "Secure",
            "timezone": "Asia/Shanghai",
        })
        assert registration.status_code == 201
        session_token = client.cookies.get("__Host-selfecho_session")
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        assert session_token and csrf
        cookie = f"__Host-selfecho_session={session_token}"
        client.headers["X-CSRF-Token"] = csrf
        draft = client.put(
            "/api/capture-draft",
            json={"revision": 0, "current_text": "original"},
            headers={"Cookie": cookie},
        ).json()["draft"]
        with client.websocket_connect(
            "/api/capture-draft/voice-stream",
            headers={"origin": "http://testserver", "Cookie": cookie},
        ) as ws:
            ws.send_json({"type": "hello", "csrf": csrf,
                          "draft_id": draft["id"], "revision": draft["revision"],
                          "client_segment_id": "stream-secure"})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            ws.send_json({"type": "stop"})
            uploaded = client.put(
                f"/api/capture-draft/streaming-voice-segments/stream-secure",
                params={"revision": draft["revision"], "draft_id": draft["id"],
                        "owner_id": 1, "session_id": ready["session_id"]},
                content=b"complete-original-audio",
                headers={"Content-Type": "audio/webm", "Cookie": cookie},
            )
            assert uploaded.status_code == 202
            while ws.receive_json()["type"] != "complete":
                pass
    assert client.get("/api/capture-draft",
                      headers={"Cookie": cookie}).json()["draft"]["current_text"] == (
        "original\nfirst sentence second sentence"
    )


def test_streaming_adapter_rejects_shared_endpoint_and_missing_key():
    from app.services.alibaba_asr import AlibabaASRConfigurationError
    from app.services.alibaba_streaming_asr import AlibabaStreamingASRSession

    for api_url, api_key in [
        ("https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
         "synthetic-key"),
        ("https://workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
         ""),
        ("http://workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
         "synthetic-key"),
        ("https://user:pass@workspace.example.invalid/api/v1/services/aigc/multimodal-generation/generation",
         "synthetic-key"),
    ]:
        with pytest.raises(AlibabaASRConfigurationError):
            AlibabaStreamingASRSession(api_url, api_key, 2)


def test_voice_disabled_instances_reject_streaming_before_provider(tmp_path):
    executable = Path(__import__("sys").executable).resolve()
    invite_code = "streaming-disabled-invite"
    settings = Settings(
        database_path=tmp_path / "disabled.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(invite_code),
        app_origin="http://testserver",
        session_cookie_secure=False,
        voice_asr_enabled=False,
    )
    app = create_app(settings=settings, ai_service=DisabledAIService())
    with TestClient(app) as client:
        registration = client.post("/api/auth/register", json={
            "invite_code": invite_code,
            "email": "disabled@example.com",
            "password": "synthetic password", "display_name": "Disabled",
            "timezone": "Asia/Shanghai",
        })
        assert registration.status_code == 201
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/api/capture-draft/voice-stream",
                headers={"origin": "http://testserver"},
            ) as ws:
                ws.receive_json()
        assert rejected.value.code == 4403
        assert app.state.voice_stream_attempts == {}


def test_streaming_upload_preflight_rejection_releases_reservation(environment):
    """A Content-Length preflight failure must not leak the upload reservation."""
    app, client, draft, providers = environment
    guard = app.state.storage_admission
    guard.voice_concurrent_limit = 1
    with _connect(client, draft, "preflight-leak") as ws:
        session_id = _hello(ws, client, draft, "preflight-leak")
        denied = client.put(
            "/api/capture-draft/streaming-voice-segments/preflight-leak",
            params={"revision": draft["revision"], "draft_id": draft["id"],
                    "owner_id": 1, "session_id": session_id},
            content=b"recorded-audio",
            headers={"Content-Type": "audio/webm",
                     "Content-Length": str(guard.voice_max_bytes + 1)},
        )
        assert denied.status_code == 413
        # The reservation forked for this upload was released exactly once.
        ws.send_json({"type": "cancel"})
    assert app.state.voice_stream_attempts == {}
    # With the attempt's own slot returned, a leaked upload fork would still
    # hold references and keep this single-slot guard exhausted.
    recovered = guard.reserve_voice()
    recovered.release()


def test_batch_only_voice_configuration_keeps_browser_capture(tmp_path):
    """Dashscope endpoint = valid batch Voice, streaming reported unavailable."""
    executable = Path(__import__("sys").executable).resolve()
    invite_code = "batch-only-invite"
    settings = Settings(
        database_path=tmp_path / "batch-only.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(invite_code),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice").resolve(),
        voice_max_upload_bytes=1024,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_api_key=SecretStr("synthetic-key"),
    )
    batch = BatchProvider()
    app = create_app(settings=settings, ai_service=DisabledAIService(),
                     voice_asr_provider=batch,
                     voice_media_processor=MediaProcessor())
    with TestClient(app) as client:
        registration = client.post("/api/auth/register", json={
            "invite_code": invite_code,
            "email": "batch-only@example.com",
            "password": "synthetic password", "display_name": "Batch",
            "timezone": "Asia/Shanghai",
        })
        assert registration.status_code == 201
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
        capability = client.get("/api/capture-draft").json()
        assert capability["voice_available"] is True
        assert capability["streaming_voice_available"] is False
        draft = client.put("/api/capture-draft", json={
            "revision": 0, "current_text": "original",
        }).json()["draft"]
        uploaded = client.put(
            "/api/capture-draft/voice-segments/browser-batch",
            params={"revision": draft["revision"]},
            content=b"complete-original-audio",
            headers={"Content-Type": "audio/webm"},
        )
        assert uploaded.status_code == 202
        deadline = time.monotonic() + 2
        while True:
            current = client.get("/api/capture-draft").json()["draft"]
            status = current["voice_segments"][0]["transcription_status"]
            if status == "succeeded":
                break
            assert time.monotonic() < deadline, "batch transcription did not finish"
            time.sleep(0.01)
        assert current["current_text"] == "original\nexplicit retry"
        assert batch.calls == 1


def test_streaming_capability_contract_across_configurations(environment):
    app, client, draft, _providers = environment
    capable = client.get("/api/capture-draft").json()
    assert capable["voice_available"] is True
    assert capable["streaming_voice_available"] is True
    assert app.state.voice_streaming_available is True
