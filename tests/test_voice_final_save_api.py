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
from app.schemas import AIExtraction, AIItemFields, ItemStatus
from app.services.ai import AIService, AIServiceError
from app.services.alibaba_asr import AlibabaASRResult
from app.services.voice_media import MediaMetadata, PreparedASRAudio


INVITE = "voice-save-test-invite"
PASSWORD = "voice save test password"


class RecordingAIService(AIService):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.database = None
        self.commit_observations: list[tuple[bool, int]] = []

    async def extract(self, original_text, existing_item):
        self.calls.append(original_text)
        if self.database is not None:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT source_draft_id FROM item_inputs
                    WHERE original_text = ? ORDER BY id DESC LIMIT 1
                    """,
                    (original_text,),
                ).fetchone()
                draft_count = connection.execute(
                    "SELECT COUNT(*) FROM capture_drafts WHERE id = ?",
                    (row["source_draft_id"],),
                ).fetchone()[0]
            self.commit_observations.append((row is not None, int(draft_count)))
        if self.fail:
            raise AIServiceError("synthetic AI failure")
        return AIExtraction(
            fields=AIItemFields(
                title="Saved Capture",
                type="note",
                status=ItemStatus.ACTIVE,
            ),
            evidence_fields=set(),
        )


class FakeASRProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio_path, *, format, sample_rate_hz):
        self.calls += 1
        return AlibabaASRResult("machine transcript", f"request-save-{self.calls}")


class FakeMediaProcessor:
    def probe(self, path: Path):
        return MediaMetadata("webm", "opus", 48_000, 1, 700, "audio/webm")

    @contextmanager
    def prepare_asr_audio(self, path: Path, metadata: MediaMetadata):
        yield PreparedASRAudio(path, "original_direct", "webm", None)


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


@pytest.fixture
def save_clients(tmp_path: Path):
    ai = RecordingAIService()
    provider = FakeASRProvider()
    executable = Path(sys.executable).resolve()
    settings = Settings(
        database_path=tmp_path / "save-api.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice-root").resolve(),
        voice_max_upload_bytes=1024,
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_api_key=SecretStr("synthetic-test-key"),
    )
    app = create_app(
        settings=settings,
        ai_service=ai,
        voice_asr_provider=provider,
        voice_media_processor=FakeMediaProcessor(),
    )
    ai.database = app.state.database
    with TestClient(app) as user_a, TestClient(app) as user_b:
        user_a_id = register(user_a, "save-a@example.com")
        user_b_id = register(user_b, "save-b@example.com")
        yield app, user_a, user_b, user_a_id, user_b_id, ai, provider


def create_draft(client: TestClient, text: str) -> dict:
    response = client.put(
        "/api/capture-draft",
        json={"current_text": text, "revision": 0},
    )
    assert response.status_code == 200
    return response.json()["draft"]


def upload_voice(client: TestClient, draft: dict, body: bytes = b"voice") -> dict:
    response = client.put(
        "/api/capture-draft/voice-segments/save-client",
        params={"revision": draft["revision"]},
        content=body,
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 202
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = client.get("/api/capture-draft").json()["draft"]
        if current["voice_segments"][0]["transcription_status"] == "succeeded":
            return current
        time.sleep(0.01)
    raise AssertionError("Voice Segment did not complete")


def save_draft(client: TestClient, draft: dict):
    return client.post(
        "/api/capture-draft/save",
        json={"draft_id": draft["id"], "revision": draft["revision"]},
    )


def test_voice_final_save_preserves_exact_text_reparents_and_is_idempotent(
    save_clients,
):
    app, user_a, _, user_a_id, _, ai, provider = save_clients
    draft = upload_voice(user_a, create_draft(user_a, "before"))
    edited_text = "  用户最终编辑文本\n保持原样  "
    edited = user_a.put(
        "/api/capture-draft",
        json={"current_text": edited_text, "revision": draft["revision"]},
    ).json()["draft"]
    payload = {"draft_id": edited["id"], "revision": edited["revision"]}

    saved = user_a.post("/api/capture-draft/save", json=payload)

    assert saved.status_code == 202
    input_id = saved.json()["id"]
    segment_id = saved.json()["voice_segment_ids"][0]
    assert saved.json()["original_text"] == edited_text
    assert saved.json()["input_method"] == "voice"
    assert saved.json()["processing_status"] == "pending"
    assert ai.calls == [edited_text]
    assert ai.commit_observations == [(True, 0)]
    assert provider.calls == 1
    assert user_a.get("/api/capture-draft").json()["draft"] is None
    with app.state.database.connection() as connection:
        item_input = connection.execute(
            "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
            (input_id, user_a_id),
        ).fetchone()
        segment = connection.execute(
            "SELECT draft_id, item_input_id FROM voice_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    assert item_input["original_text"] == edited_text
    assert item_input["source_draft_id"] == edited["id"]
    assert item_input["processing_status"] == "succeeded"
    assert tuple(segment) == (None, input_id)
    assert user_a.get(f"/api/voice-segments/{segment_id}/audio").status_code == 200

    recovered = user_a.post("/api/capture-draft/save", json=payload)
    assert recovered.status_code == 202
    assert recovered.json()["id"] == input_id
    assert recovered.json()["processing_status"] == "succeeded"
    assert recovered.json()["voice_segment_ids"] == [segment_id]
    assert ai.calls == [edited_text]
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE user_id = ?",
            (user_a_id,),
        ).fetchone()[0] == 1


def test_text_only_final_save_works_when_voice_disabled_and_keeps_exact_text(
    tmp_path: Path,
):
    ai = RecordingAIService()
    settings = Settings(
        database_path=tmp_path / "text-only.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        voice_asr_enabled=False,
    )
    app = create_app(settings=settings, ai_service=ai)
    ai.database = app.state.database
    with TestClient(app) as client:
        register(client, "text-only@example.com")
        draft = create_draft(client, "\n  text-only final  \t")

        response = save_draft(client, draft)

        assert response.status_code == 202
        assert response.json()["input_method"] == "text"
        assert response.json()["original_text"] == "\n  text-only final  \t"
        assert response.json()["voice_segment_ids"] == []
        assert ai.calls == ["\n  text-only final  \t"]
        assert ai.commit_observations == [(True, 0)]


def _make_unresolved_segment(app, user_id: int, draft: dict, state: str) -> int:
    saved = app.state.voice_storage.store_original([b"unresolved audio"])
    segment = app.state.voice_repository.create_pending_segment(
        user_id=user_id,
        client_segment_id=f"unresolved-{state}",
        saved=saved,
        client_content_type="audio/webm",
        expected_revision=draft["revision"],
    ).segment
    if state in {"transcribing", "failed"}:
        assert app.state.voice_repository.claim_pending_segment(segment.id, user_id)
    if state == "failed":
        assert app.state.voice_repository.mark_segment_failed(
            segment.id,
            user_id,
            failure_code="network",
            failure_message="safe failure",
        )
    return segment.id


@pytest.mark.parametrize("segment_state", ["pending", "transcribing", "failed"])
def test_final_save_blocks_all_unresolved_voice_without_ai(
    save_clients,
    segment_state: str,
):
    app, user_a, _, user_a_id, _, ai, _ = save_clients
    draft = create_draft(user_a, "not ready")
    _make_unresolved_segment(app, user_a_id, draft, segment_state)

    response = save_draft(user_a, draft)

    assert response.status_code == 409
    assert ai.calls == []
    assert user_a.get("/api/capture-draft").json()["draft"] is not None
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs"
        ).fetchone()[0] == 0


def test_final_save_requires_owner_revision_nonblank_and_auth_without_ai(
    save_clients,
):
    app, user_a, user_b, _, _, ai, _ = save_clients
    blank = create_draft(user_a, "   \n\t")

    assert save_draft(user_a, blank).status_code == 409
    assert user_a.post(
        "/api/capture-draft/save",
        json={"draft_id": blank["id"], "revision": blank["revision"] + 1},
    ).status_code == 409
    assert user_b.post(
        "/api/capture-draft/save",
        json={"draft_id": blank["id"], "revision": blank["revision"]},
    ).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.post(
            "/api/capture-draft/save",
            json={"draft_id": blank["id"], "revision": blank["revision"]},
        ).status_code == 401
    assert ai.calls == []


@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_final_save_bad_csrf_never_commits_or_calls_ai(
    save_clients,
    token: str | None,
):
    app, user_a, _, user_a_id, _, ai, _ = save_clients
    draft = create_draft(user_a, "retain on csrf failure")
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token

    response = save_draft(user_a, draft)

    assert response.status_code == 403
    assert ai.calls == []
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE user_id = ?",
            (user_a_id,),
        ).fetchone()[0] == 0
    user_a.headers["X-CSRF-Token"] = valid
    assert user_a.get("/api/capture-draft").json()["draft"] is not None


def test_committed_but_uncertain_retry_recovers_and_schedules_ai_once(save_clients):
    app, user_a, _, user_a_id, _, ai, _ = save_clients
    draft = create_draft(user_a, "uncertain response")
    committed = app.state.voice_repository.save_draft(
        user_a_id,
        draft["id"],
        draft["revision"],
    )
    assert committed.created is True
    assert committed.item_input.processing_status.value == "pending"
    assert ai.calls == []

    recovered = save_draft(user_a, draft)

    assert recovered.status_code == 202
    assert recovered.json()["id"] == committed.item_input.id
    assert ai.calls == ["uncertain response"]
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE user_id = ?",
            (user_a_id,),
        ).fetchone()[0] == 1


def test_ai_failure_keeps_input_draft_removal_and_voice_association(save_clients):
    app, user_a, user_b, user_a_id, _, ai, _ = save_clients
    ai.fail = True
    draft = upload_voice(user_a, create_draft(user_a, "will fail AI"))
    response = save_draft(user_a, draft)
    input_id = response.json()["id"]
    segment_id = response.json()["voice_segment_ids"][0]

    assert response.status_code == 202
    assert ai.calls == ["will fail AI\nmachine transcript"]
    assert user_a.get("/api/capture-draft").json()["draft"] is None
    with app.state.database.connection() as connection:
        item_input = connection.execute(
            "SELECT processing_status FROM item_inputs WHERE id = ? AND user_id = ?",
            (input_id, user_a_id),
        ).fetchone()
        segment = connection.execute(
            "SELECT draft_id, item_input_id FROM voice_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    assert item_input["processing_status"] == "failed"
    assert tuple(segment) == (None, input_id)
    assert user_a.get(f"/api/voice-segments/{segment_id}/audio").status_code == 200
    assert user_b.delete(f"/api/inputs/{input_id}").status_code == 404


@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_failed_input_delete_bad_csrf_keeps_input_and_audio(
    save_clients,
    token: str | None,
):
    app, user_a, _, user_a_id, _, ai, _ = save_clients
    ai.fail = True
    draft = upload_voice(user_a, create_draft(user_a, "delete csrf"))
    saved = save_draft(user_a, draft)
    input_id = saved.json()["id"]
    original = next(app.state.voice_storage.original_root.rglob("*.bin"))
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token

    response = user_a.delete(f"/api/inputs/{input_id}")

    assert response.status_code == 403
    assert original.is_file()
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = ? AND user_id = ?",
            (input_id, user_a_id),
        ).fetchone()[0] == 1
    user_a.headers["X-CSRF-Token"] = valid


def test_owner_can_delete_failed_unlinked_voice_input_via_ledger(save_clients):
    app, user_a, user_b, _, _, ai, _ = save_clients
    ai.fail = True
    draft = upload_voice(user_a, create_draft(user_a, "delete failed"))
    saved = save_draft(user_a, draft)
    input_id = saved.json()["id"]
    segment_id = saved.json()["voice_segment_ids"][0]
    assert len(list(app.state.voice_storage.original_root.rglob("*.bin"))) == 1

    assert user_b.delete(f"/api/inputs/{input_id}").status_code == 404
    deleted = user_a.delete(f"/api/inputs/{input_id}")

    assert deleted.status_code == 204
    assert user_a.get(f"/api/voice-segments/{segment_id}/audio").status_code == 404
    assert list(app.state.voice_storage.original_root.rglob("*.bin")) == []
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_failed_input_delete_drain_failure_keeps_retryable_ledger(
    save_clients,
    monkeypatch: pytest.MonkeyPatch,
):
    app, user_a, _, _, _, ai, _ = save_clients
    ai.fail = True
    draft = upload_voice(user_a, create_draft(user_a, "ledger failure"))
    saved = save_draft(user_a, draft)
    input_id = saved.json()["id"]
    original_path = next(app.state.voice_storage.original_root.rglob("*.bin"))

    def fail_delete(storage_key: str) -> bool:
        raise PermissionError("synthetic lock")

    monkeypatch.setattr(app.state.voice_storage, "delete_original", fail_delete)
    response = user_a.delete(f"/api/inputs/{input_id}")

    assert response.status_code == 204
    assert original_path.is_file()
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = ?",
            (input_id,),
        ).fetchone()[0] == 0
        deletion = connection.execute(
            "SELECT reason, attempt_count, last_error FROM voice_file_deletions"
        ).fetchone()
    assert deletion["reason"] == "failed_input_delete"
    assert deletion["attempt_count"] == 1
    assert "PermissionError" in deletion["last_error"]


def test_failed_input_delete_rejects_healthy_or_linked_input(save_clients):
    app, user_a, _, _, _, ai, _ = save_clients
    draft = create_draft(user_a, "healthy")
    saved = save_draft(user_a, draft)
    assert user_a.delete(f"/api/inputs/{saved.json()['id']}").status_code == 409
    assert ai.calls == ["healthy"]
    with app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = ?",
            (saved.json()["id"],),
        ).fetchone()[0] == 1
