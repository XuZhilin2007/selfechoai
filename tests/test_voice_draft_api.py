from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.services.ai import DisabledAIService


INVITE = "voice-draft-test-invite"
PASSWORD = "voice draft test password"
NOW = "2026-09-07T01:00:00+00:00"


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
def draft_clients(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "draft-api.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
    )
    app = create_app(settings=settings, ai_service=DisabledAIService())
    with TestClient(app) as user_a, TestClient(app) as user_b:
        user_a_id = register(user_a, "draft-a@example.com")
        user_b_id = register(user_b, "draft-b@example.com")
        yield app, user_a, user_b, user_a_id, user_b_id


def test_draft_load_create_update_preserves_exact_text(draft_clients):
    _, user_a, _, _, _ = draft_clients

    assert user_a.get("/api/capture-draft").json() == {
        "draft": None,
        "voice_available": False,
    }
    created_response = user_a.put(
        "/api/capture-draft",
        json={"current_text": "  exact\ntext  ", "revision": 0},
    )

    assert created_response.status_code == 200
    created = created_response.json()["draft"]
    assert created["current_text"] == "  exact\ntext  "
    assert created["revision"] == 1
    assert created["voice_segments"] == []

    updated_response = user_a.put(
        "/api/capture-draft",
        json={"current_text": "\n revised exactly \t", "revision": 1},
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()["draft"]
    assert updated["current_text"] == "\n revised exactly \t"
    assert updated["revision"] == 2
    assert user_a.get("/api/capture-draft").json()["draft"] == updated


def test_draft_revision_conflict_never_overwrites_server_text(draft_clients):
    _, user_a, _, _, _ = draft_clients
    created = user_a.put(
        "/api/capture-draft",
        json={"current_text": "one", "revision": 0},
    ).json()["draft"]
    newer = user_a.put(
        "/api/capture-draft",
        json={"current_text": "two", "revision": created["revision"]},
    ).json()["draft"]

    conflict = user_a.put(
        "/api/capture-draft",
        json={
            "current_text": "must not win",
            "revision": created["revision"],
        },
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Capture Draft revision is stale"}
    assert user_a.get("/api/capture-draft").json()["draft"] == newer


def test_draft_api_is_session_scoped_and_never_targets_another_user(
    draft_clients,
):
    _, user_a, user_b, _, _ = draft_clients
    draft_a = user_a.put(
        "/api/capture-draft",
        json={"current_text": "private A", "revision": 0},
    ).json()["draft"]
    draft_b = user_b.put(
        "/api/capture-draft",
        json={"current_text": "private B", "revision": 0},
    ).json()["draft"]

    assert user_a.get("/api/capture-draft").json()["draft"] == draft_a
    assert user_b.get("/api/capture-draft").json()["draft"] == draft_b

    changed_b = user_b.put(
        "/api/capture-draft",
        json={"current_text": "only B changed", "revision": draft_b["revision"]},
    )
    assert changed_b.status_code == 200
    assert user_a.get("/api/capture-draft").json()["draft"] == draft_a


@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_draft_put_rejects_missing_or_invalid_csrf_without_mutation(
    draft_clients,
    token: str | None,
):
    _, user_a, _, _, _ = draft_clients
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token

    response = user_a.put(
        "/api/capture-draft",
        json={"current_text": "must not persist", "revision": 0},
    )

    assert response.status_code == 403
    user_a.headers["X-CSRF-Token"] = valid
    assert user_a.get("/api/capture-draft").json()["draft"] is None


@pytest.mark.parametrize("token", [None, "invalid-csrf-token"])
def test_draft_discard_rejects_missing_or_invalid_csrf_without_mutation(
    draft_clients,
    token: str | None,
):
    _, user_a, _, _, _ = draft_clients
    draft = user_a.put(
        "/api/capture-draft",
        json={"current_text": "retain me", "revision": 0},
    ).json()["draft"]
    valid = user_a.headers.pop("X-CSRF-Token")
    if token is not None:
        user_a.headers["X-CSRF-Token"] = token

    response = user_a.delete(
        "/api/capture-draft",
        params={"revision": draft["revision"]},
    )

    assert response.status_code == 403
    user_a.headers["X-CSRF-Token"] = valid
    assert user_a.get("/api/capture-draft").json()["draft"] == draft


def test_draft_discard_is_revision_guarded_and_missing_draft_is_idempotent(
    draft_clients,
):
    _, user_a, user_b, _, _ = draft_clients
    draft_a = user_a.put(
        "/api/capture-draft",
        json={"current_text": "A", "revision": 0},
    ).json()["draft"]
    draft_b = user_b.put(
        "/api/capture-draft",
        json={"current_text": "B", "revision": 0},
    ).json()["draft"]

    stale = user_a.delete(
        "/api/capture-draft",
        params={"revision": draft_a["revision"] + 1},
    )
    assert stale.status_code == 409
    assert user_a.get("/api/capture-draft").json()["draft"] == draft_a

    assert user_a.delete(
        "/api/capture-draft",
        params={"revision": draft_a["revision"]},
    ).status_code == 204
    assert user_a.delete(
        "/api/capture-draft",
        params={"revision": draft_a["revision"]},
    ).status_code == 204
    assert user_a.get("/api/capture-draft").json()["draft"] is None
    assert user_b.get("/api/capture-draft").json()["draft"] == draft_b


def test_draft_api_requires_auth_and_enforces_schema_limits(draft_clients):
    app, user_a, _, _, _ = draft_clients
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/capture-draft").status_code == 401
        assert anonymous.put(
            "/api/capture-draft",
            json={"current_text": "x", "revision": 0},
        ).status_code == 401
        assert anonymous.delete(
            "/api/capture-draft",
            params={"revision": 0},
        ).status_code == 401

    assert user_a.put(
        "/api/capture-draft",
        json={"current_text": "x" * 10_001, "revision": 0},
    ).status_code == 422
    assert user_a.put(
        "/api/capture-draft",
        json={"current_text": "", "revision": 0, "user_id": 999},
    ).status_code == 422
    assert user_a.get("/api/capture-draft").json()["draft"] is None


def test_enabled_discard_ledgers_then_physically_deletes_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = Path(sys.executable).resolve()
    settings = Settings(
        database_path=tmp_path / "enabled.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        voice_asr_enabled=True,
        voice_storage_root=(tmp_path / "voice-root").resolve(),
        ffprobe_path=executable,
        ffmpeg_path=executable,
        alibaba_api_key=SecretStr("synthetic-test-key"),
    )
    app = create_app(settings=settings, ai_service=DisabledAIService())
    ai_schedule_calls: list[tuple[int, int]] = []

    async def record_unexpected_ai(input_id: int, user_id: int) -> None:
        ai_schedule_calls.append((input_id, user_id))

    monkeypatch.setattr(app.state.processor, "process_input", record_unexpected_ai)
    with TestClient(app) as client:
        user_id = register(client, "enabled-draft@example.com")
        draft = client.put(
            "/api/capture-draft",
            json={"current_text": "voice", "revision": 0},
        ).json()["draft"]
        saved = app.state.voice_storage.store_original([b"synthetic audio"])
        with app.state.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO voice_segments (
                    user_id, draft_id, position, client_segment_id, storage_key,
                    original_size_bytes, original_sha256, transcription_status,
                    provider, model, failure_code, failure_message,
                    created_time, updated_time, transcription_finished_time
                ) VALUES (?, ?, 0, 'client-delete', ?, ?, ?, 'failed',
                          'alibaba', 'qwen-audio-3.0-asr-flash', 'network',
                          'safe failure', ?, ?, ?)
                """,
                (
                    user_id,
                    draft["id"],
                    saved.storage_key,
                    saved.size_bytes,
                    saved.sha256,
                    NOW,
                    NOW,
                    NOW,
                ),
            )

        response = client.delete(
            "/api/capture-draft",
            params={"revision": draft["revision"]},
        )

        assert response.status_code == 204
        assert ai_schedule_calls == []
        assert not saved.path.exists()
        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM capture_drafts"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM voice_segments"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM voice_file_deletions"
            ).fetchone()[0] == 0
