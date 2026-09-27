from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.services.ai import DisabledAIService


INVITE_CODE = "delta-invite-code"
PASSWORD = "a strong test password"


def settings(tmp_path: Path, mode: str) -> Settings:
    return Settings(
        database_path=tmp_path / "modes.db",
        registration_mode=mode,
        invite_code_hash=(
            hash_invite_code(INVITE_CODE) if mode == "invite" else ""
        ),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
    )


def register(client: TestClient, email: str, **extra):
    payload = {
        "email": email,
        "password": PASSWORD,
        "display_name": "User",
        "timezone": "UTC",
        **extra,
    }
    return client.post("/api/auth/register", json=payload)


def test_open_mode_registers_without_invite_code_and_signs_in(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "open"))) as client:
        response = register(client, "open-user@example.com")
        assert response.status_code == 201
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "open-user@example.com"


def test_open_mode_ignores_a_supplied_invite_code(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "open"))) as client:
        assert register(
            client, "open-two@example.com", invite_code="anything-at-all"
        ).status_code == 201


def test_open_mode_duplicate_email_still_conflicts(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "open"))) as client:
        assert register(client, "dup@example.com").status_code == 201
        assert register(client, "dup@example.com").status_code == 409


def test_open_mode_registration_is_still_admission_limited(tmp_path: Path):
    limited_settings = replace(
        settings(tmp_path, "open"), auth_registration_source_limit=1
    )
    with TestClient(create_app(limited_settings)) as client:
        assert register(client, "first@example.com").status_code == 201
        limited = register(client, "second@example.com")
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60" 


def test_invite_mode_missing_code_is_rejected_without_admission_bypass(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "invite"))) as client:
        missing = register(client, "no-code@example.com")
        assert missing.status_code == 403
        wrong = register(client, "no-code@example.com", invite_code="wrong-code")
        assert wrong.status_code == 403


def test_invite_mode_with_valid_code_still_works(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "invite"))) as client:
        assert register(
            client, "invited@example.com", invite_code=INVITE_CODE
        ).status_code == 201


def test_closed_mode_rejects_plain_registration(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path, "closed"))) as client:
        response = register(client, "closed@example.com", invite_code="")
        assert response.status_code == 403


@pytest.mark.parametrize("mode", ["closed", "invite", "open"])
def test_registration_config_reports_the_real_mode(tmp_path: Path, mode: str):
    with TestClient(create_app(settings(tmp_path, mode))) as client:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json() == {"registration_mode": mode}


def test_invalid_registration_mode_fails_closed(tmp_path: Path):
    from app.auth import AuthenticationService
    from app.auth_repository import AuthRepository
    from app.database import Database

    database = Database(tmp_path / "bad.db")
    database.initialize()
    with pytest.raises(ValueError, match="closed, invite, or open"):
        AuthenticationService(
            AuthRepository(database),
            registration_mode="public",
            invite_code_hash="",
            session_expiration_seconds=3600,
        )
