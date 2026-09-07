from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.services.ai import DisabledAIService
from app.services.alibaba_asr import AlibabaASRNetworkError, AlibabaASRResult
from app.services.voice_media import MediaMetadata, PreparedASRAudio
from app.services.voice_storage import VoiceStorageError


INVITE = "voice-segment-test-invite"
PASSWORD = "voice segment test password"


class FakeMediaProcessor:
    def probe(self, path: Path) -> MediaMetadata:
        assert path.read_bytes()
        return MediaMetadata("webm", "opus", 48_000, 1, 850, "audio/webm")

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        yield PreparedASRAudio(path, "original_direct", "webm", None)


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None
        self.transcript = "API 机器转写"

    async def transcribe(self, audio_path, *, format, sample_rate_hz):
        self.calls += 1
        assert audio_path.read_bytes()
        assert format == "webm"
        assert sample_rate_hz is None
        if self.error is not None:
            raise self.error
        return AlibabaASRResult(self.transcript, f"request-{self.calls}")


def register(client: TestClient, email: str) -> int:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE,
            "email": email,
            "password": PASSWORD,
            "display_name": email.split("@", 1)[0],
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    client.headers["X-CSRF-Token"] = csrf
    return response.json()["id"]


def login(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    client.headers["X-CSRF-Token"] = csrf


@pytest.fixture
def voice_clients(tmp_path: Path):
    provider = FakeProvider()
    executable = Path(sys.executable).resolve()
    settings = Settings(
        database_path=tmp_path / "voice-api.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice-root").resolve(),
        voice_max_upload_bytes=64,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_api_key=SecretStr("synthetic-test-key"),
    )
    app = create_app(
        settings=settings,
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=FakeMediaProcessor(),
    )
    with TestClient(app) as user_a, TestClient(app) as user_b:
        user_a_id = register(user_a, "voice-a@example.com")
        user_b_id = register(user_b, "voice-b@example.com")
        yield app, user_a, user_b, user_a_id, user_b_id, provider


def create_draft(client: TestClient, text: str = "before") -> dict:
    response = client.put(
        "/api/capture-draft",
        json={"current_text": text, "revision": 0},
    )
    assert response.status_code == 200
    return response.json()["draft"]


def upload(client: TestClient, client_id: str, body, revision: int, **headers):
    request_headers = {"Content-Type": "audio/webm;codecs=opus", **headers}
    return client.put(
        f"/api/capture-draft/voice-segments/{client_id}",
        params={"revision": revision},
        content=body,
        headers=request_headers,
    )


def wait_for_segment(client: TestClient, status: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        draft = client.get("/api/capture-draft").json()["draft"]
        if draft and draft["voice_segments"]:
            segment = draft["voice_segments"][0]
            if segment["transcription_status"] == status:
                return segment
        time.sleep(0.01)
    raise AssertionError(f"Voice Segment did not reach {status}")


def test_raw_streaming_upload_is_durable_idempotent_and_scheduled_once(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, user_a_id, _, provider = voice_clients
    draft = create_draft(user_a)
    schedule_calls: list[tuple[int, int]] = []
    original_schedule = app.state.voice_runtime.schedule

    def recording_schedule(segment_id: int, user_id: int) -> bool:
        schedule_calls.append((segment_id, user_id))
        return original_schedule(segment_id, user_id)

    monkeypatch.setattr(app.state.voice_runtime, "schedule", recording_schedule)
    body = b"synthetic-webm"
    response = upload(
        user_a,
        "client-1",
        iter([b"synthetic-", b"webm"]),
        draft["revision"],
        **{"Content-Type": "audio/mpeg"},
    )

    assert response.status_code == 202
    payload = response.json()
    segment_id = payload["id"]
    assert payload["transcription_status"] == "pending"
    assert payload["client_content_type"] == "audio/mpeg"
    assert "storage_key" not in payload
    assert schedule_calls == [(segment_id, user_a_id)]
    completed = wait_for_segment(user_a, "succeeded")
    assert completed["detected_container"] == "webm"
    assert completed["provider_transcript"] == "API 机器转写"
    assert provider.calls == 1
    updated = user_a.get("/api/capture-draft").json()["draft"]
    assert updated["current_text"] == "before\nAPI 机器转写"
    originals = list(app.state.voice_storage.original_root.rglob("*.bin"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == body

    same = upload(user_a, "client-1", body, updated["revision"])
    assert same.status_code == 202
    assert same.json()["id"] == segment_id
    assert provider.calls == 1
    assert schedule_calls == [(segment_id, user_a_id)]

    conflict = upload(user_a, "client-1", b"different", updated["revision"])
    assert conflict.status_code == 409
    assert provider.calls == 1
    assert schedule_calls == [(segment_id, user_a_id)]
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions WHERE reason = 'orphan_cleanup'"
        ).fetchone()[0] == 2


@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_upload_rejects_missing_or_invalid_csrf_before_storage(
    voice_clients,
    token: str | None,
):
    app, user_a, _, _, _, provider = voice_clients
    draft = create_draft(user_a)
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token

    response = upload(user_a, "csrf-upload", b"audio", draft["revision"])

    assert response.status_code == 403
    assert provider.calls == 0
    assert list(app.state.voice_storage.original_root.rglob("*.bin")) == []
    user_a.headers["X-CSRF-Token"] = valid


def test_upload_validates_auth_draft_revision_size_and_client_id(voice_clients):
    app, user_a, user_b, _, _, provider = voice_clients
    draft = create_draft(user_a)

    anonymous = TestClient(app)
    assert upload(
        anonymous,
        "anonymous-1",
        b"audio",
        draft["revision"],
    ).status_code == 401

    assert upload(user_b, "no-draft", b"audio", 0).status_code == 404
    assert upload(
        user_a,
        "stale-1",
        b"audio",
        draft["revision"] + 1,
    ).status_code == 409
    assert upload(user_a, "invalid space", b"audio", draft["revision"]).status_code == 422
    assert upload(user_a, "empty", b"", draft["revision"]).status_code == 400
    assert upload(user_a, "too-large", b"x" * 65, draft["revision"]).status_code == 413
    malformed = upload(
        user_a,
        "bad-length",
        b"audio",
        draft["revision"],
        **{"Content-Length": "not-a-number"},
    )
    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid Content-Length"}

    actual_oversized = upload(
        user_a,
        "actual-too-large",
        iter([b"x" * 33, b"x" * 32]),
        draft["revision"],
    )
    assert actual_oversized.status_code == 413

    exact = upload(user_a, "exact-limit", b"x" * 64, draft["revision"])
    assert exact.status_code == 202
    wait_for_segment(user_a, "succeeded")
    assert provider.calls == 1


def test_runtime_scheduling_failure_keeps_durable_pending_and_blocks_next_upload(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, user_a_id, _, provider = voice_clients
    draft = create_draft(user_a)

    def fail_schedule(segment_id: int, user_id: int) -> bool:
        raise RuntimeError("synthetic scheduler failure")

    monkeypatch.setattr(app.state.voice_runtime, "schedule", fail_schedule)
    response = upload(user_a, "pending-safe", b"audio", draft["revision"])

    assert response.status_code == 202
    segment_id = response.json()["id"]
    persisted = app.state.voice_repository.get_segment(segment_id, user_a_id)
    assert persisted.transcription_status == "pending"
    assert provider.calls == 0
    assert len(list(app.state.voice_storage.original_root.rglob("*.bin"))) == 1
    assert upload(
        user_a,
        "blocked-next",
        b"next",
        draft["revision"],
    ).status_code == 409


def test_storage_failure_is_controlled_and_does_not_register_segment(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, _, _, provider = voice_clients
    draft = create_draft(user_a)

    async def fail_store(chunks, *, storage_key):
        raise VoiceStorageError("private-path-must-not-leak")

    monkeypatch.setattr(app.state.voice_storage, "store_original_async", fail_store)
    response = upload(user_a, "storage-fail", b"audio", draft["revision"])

    assert response.status_code == 500
    assert response.json() == {"detail": "Original Audio could not be stored safely"}
    assert "private-path" not in response.text
    assert provider.calls == 0
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments"
        ).fetchone()[0] == 0


def test_db_registration_failure_keeps_orphan_intent_and_safe_error(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, _, _, provider = voice_clients
    draft = create_draft(user_a)

    def fail_registration(**kwargs):
        raise RuntimeError("SQL and path details must not leak")

    monkeypatch.setattr(
        app.state.voice_repository,
        "create_pending_segment",
        fail_registration,
    )
    response = upload(user_a, "db-fail", b"audio", draft["revision"])

    assert response.status_code == 500
    assert response.json() == {"detail": "Voice Segment could not be registered safely"}
    assert "SQL" not in response.text
    assert provider.calls == 0
    assert len(list(app.state.voice_storage.original_root.rglob("*.bin"))) == 1
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments"
        ).fetchone()[0] == 0
        deletion = connection.execute(
            "SELECT reason FROM voice_file_deletions"
        ).fetchone()
    assert deletion["reason"] == "orphan_cleanup"


def _create_failed_segment(voice_clients) -> tuple:
    app, user_a, user_b, user_a_id, user_b_id, provider = voice_clients
    provider.error = AlibabaASRNetworkError("synthetic failure")
    draft = create_draft(user_a)
    response = upload(user_a, "failed-segment", b"0123456789abcdef", draft["revision"])
    assert response.status_code == 202
    segment = wait_for_segment(user_a, "failed")
    return app, user_a, user_b, user_a_id, user_b_id, provider, draft, segment


def test_audio_playback_is_owner_only_private_and_supports_single_ranges(
    voice_clients,
):
    app, user_a, user_b, _, _, _, _, segment = _create_failed_segment(voice_clients)
    segment_id = segment["id"]
    body = b"0123456789abcdef"

    full = user_a.get(f"/api/voice-segments/{segment_id}/audio")
    assert full.status_code == 200
    assert full.content == body
    assert full.headers["content-type"] == "audio/webm"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["x-content-type-options"] == "nosniff"
    assert full.headers["content-disposition"] == "inline"
    assert full.headers["content-length"] == str(len(body))
    assert "original/" not in full.text

    cases = {
        "bytes=3-7": body[3:8],
        "bytes=-4": body[-4:],
        "bytes=5-": body[5:],
        "bytes=0-999": body,
    }
    for range_value, expected in cases.items():
        partial = user_a.get(
            f"/api/voice-segments/{segment_id}/audio",
            headers={"Range": range_value},
        )
        assert partial.status_code == 206
        assert partial.content == expected
        assert partial.headers["cache-control"] == "private, no-store"
        assert partial.headers["x-content-type-options"] == "nosniff"
        assert partial.headers["content-disposition"] == "inline"

    for invalid in (
        "bytes=99-100",
        "bytes=5-2",
        "bytes=-0",
        "bytes=abc-def",
        "bytes=",
        "bytes=--",
        "items=0-1",
        "bytes=0-1,3-4",
    ):
        response = user_a.get(
            f"/api/voice-segments/{segment_id}/audio",
            headers={"Range": invalid},
        )
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(body)}"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    with TestClient(app) as anonymous:
        assert anonymous.get(
            f"/api/voice-segments/{segment_id}/audio"
        ).status_code == 401
    assert user_b.get(f"/api/voice-segments/{segment_id}/audio").status_code == 404


def test_missing_physical_audio_is_safe_404_without_path_leak(voice_clients):
    app, user_a, _, user_a_id, _, _, _, segment = _create_failed_segment(voice_clients)
    record = app.state.voice_repository.get_segment_record(segment["id"], user_a_id)
    path = app.state.voice_storage.resolve_original(record.storage_key)
    path.unlink()

    response = user_a.get(f"/api/voice-segments/{segment['id']}/audio")

    assert response.status_code == 404
    assert response.json() == {"detail": "Original Audio not found"}
    assert str(path) not in response.text
    assert record.storage_key not in response.text


@pytest.mark.parametrize("operation", ["retry", "delete"])
@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_retry_and_delete_reject_bad_csrf_without_mutation(
    voice_clients,
    operation: str,
    token: str | None,
):
    app, user_a, _, user_a_id, _, provider, _, segment = _create_failed_segment(
        voice_clients
    )
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token
    path = f"/api/voice-segments/{segment['id']}"

    response = user_a.post(f"{path}/retry") if operation == "retry" else user_a.delete(path)

    assert response.status_code == 403
    current = app.state.voice_repository.get_segment(segment["id"], user_a_id)
    assert current.transcription_status == "failed"
    assert provider.calls == 1
    user_a.headers["X-CSRF-Token"] = valid


def test_retry_and_delete_are_owner_scoped_state_gated_and_separate_from_drain(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, user_b, user_a_id, _, provider, _, segment = _create_failed_segment(
        voice_clients
    )
    segment_id = segment["id"]

    assert user_b.post(f"/api/voice-segments/{segment_id}/retry").status_code == 404
    assert user_b.delete(f"/api/voice-segments/{segment_id}").status_code == 404

    provider.error = None
    schedule_calls: list[tuple[int, int]] = []
    original_schedule = app.state.voice_runtime.schedule

    def recording_schedule(candidate_segment_id: int, candidate_user_id: int) -> bool:
        schedule_calls.append((candidate_segment_id, candidate_user_id))
        return original_schedule(candidate_segment_id, candidate_user_id)

    monkeypatch.setattr(app.state.voice_runtime, "schedule", recording_schedule)
    retry = user_a.post(f"/api/voice-segments/{segment_id}/retry")
    assert retry.status_code == 202
    assert retry.json()["transcription_status"] == "pending"
    succeeded = wait_for_segment(user_a, "succeeded")
    assert succeeded["attempt_count"] == 2
    assert provider.calls == 2
    assert schedule_calls == [(segment_id, user_a_id)]
    assert user_a.delete(f"/api/voice-segments/{segment_id}").status_code == 409

    with app.state.database.transaction() as connection:
        connection.execute(
            """
            UPDATE voice_segments
            SET transcription_status = 'failed', provider_transcript = NULL,
                failure_code = 'network', failure_message = 'safe failure',
                transcription_finished_time = updated_time
            WHERE id = ? AND user_id = ?
            """,
            (segment_id, user_a_id),
        )
    assert user_a.delete(f"/api/voice-segments/{segment_id}").status_code == 204
    assert user_a.get(f"/api/voice-segments/{segment_id}/audio").status_code == 404
    assert list(app.state.voice_storage.original_root.rglob("*.bin")) == []


def test_failed_segment_delete_does_not_depend_on_physical_drain(
    voice_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, user_a_id, _, _, _, segment = _create_failed_segment(
        voice_clients
    )
    segment_id = segment["id"]
    original_path = next(app.state.voice_storage.original_root.rglob("*.bin"))

    def fail_delete(storage_key: str) -> bool:
        raise PermissionError("synthetic lock")

    monkeypatch.setattr(app.state.voice_storage, "delete_original", fail_delete)

    response = user_a.delete(f"/api/voice-segments/{segment_id}")

    assert response.status_code == 204
    assert original_path.is_file()
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE id = ? AND user_id = ?",
            (segment_id, user_a_id),
        ).fetchone()[0] == 0
        deletion = connection.execute(
            """
            SELECT reason, attempt_count, last_error
            FROM voice_file_deletions
            """
        ).fetchone()
    assert deletion["reason"] == "segment_delete"
    assert deletion["attempt_count"] == 1
    assert "PermissionError" in deletion["last_error"]


def test_draft_text_limit_retry_reuses_paid_transcript_without_provider_call(
    voice_clients,
):
    _, user_a, _, _, _, provider = voice_clients
    provider.transcript = "XY"
    draft = create_draft(user_a, "a" * 9_999)
    response = upload(user_a, "limit-reuse", b"audio", draft["revision"])
    assert response.status_code == 202
    failed = wait_for_segment(user_a, "failed")
    assert failed["failure_code"] == "draft_text_limit"
    assert provider.calls == 1

    current = user_a.get("/api/capture-draft").json()["draft"]
    shortened = user_a.put(
        "/api/capture-draft",
        json={"current_text": "short", "revision": current["revision"]},
    ).json()["draft"]
    assert shortened["current_text"] == "short"

    retry = user_a.post(f"/api/voice-segments/{failed['id']}/retry")
    assert retry.status_code == 202
    assert retry.json()["transcription_status"] == "succeeded"
    assert provider.calls == 1
    completed = user_a.get("/api/capture-draft").json()["draft"]
    assert completed["current_text"] == "short\nXY"


def test_disabled_voice_rejects_upload_and_retry_but_uses_owner_first_for_audio(
    tmp_path: Path,
):
    database_path = tmp_path / "disabled-history.db"
    storage_root = (tmp_path / "voice-history").resolve()
    executable = Path(sys.executable).resolve()
    enabled = Settings(
        database_path=database_path,
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        voice_asr_enabled=True,
        voice_storage_root=storage_root,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_api_key=SecretStr("synthetic-test-key"),
    )
    provider = FakeProvider()
    provider.error = AlibabaASRNetworkError("synthetic failure")
    enabled_app = create_app(
        settings=enabled,
        ai_service=DisabledAIService(),
        voice_asr_provider=provider,
        voice_media_processor=FakeMediaProcessor(),
    )
    with TestClient(enabled_app) as owner, TestClient(enabled_app) as other:
        register(owner, "history-owner@example.com")
        register(other, "history-other@example.com")
        draft = create_draft(owner)
        uploaded = upload(owner, "history-segment", b"audio", draft["revision"])
        segment_id = uploaded.json()["id"]
        wait_for_segment(owner, "failed")

    disabled = Settings(
        database_path=database_path,
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        voice_asr_enabled=False,
    )
    disabled_app = create_app(settings=disabled, ai_service=DisabledAIService())
    with TestClient(disabled_app) as owner, TestClient(disabled_app) as other:
        login(owner, "history-owner@example.com")
        login(other, "history-other@example.com")
        owner_draft = owner.get("/api/capture-draft").json()["draft"]
        assert upload(
            owner,
            "disabled-new",
            b"audio",
            owner_draft["revision"],
        ).status_code == 503
        assert owner.post(f"/api/voice-segments/{segment_id}/retry").status_code == 503
        assert owner.get(f"/api/voice-segments/{segment_id}/audio").status_code == 503
        assert other.get(f"/api/voice-segments/{segment_id}/audio").status_code == 404
