from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code, hash_session_token
from app.auth_routes import (
    CSRF_COOKIE_NAME,
    LOCAL_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from app.config import Settings
from app.main import create_app


INVITE_CODE = "selfecho-test-invite"
PASSWORD = "a strong test password"


def auth_settings(
    database_path: Path,
    *,
    registration_mode: str = "invite",
    secure_cookie: bool = False,
) -> Settings:
    return Settings(
        database_path=database_path,
        registration_mode=registration_mode,
        invite_code_hash=(
            hash_invite_code(INVITE_CODE) if registration_mode == "invite" else ""
        ),
        app_origin=(
            "https://selfechoai.com"
            if secure_cookie
            else "http://127.0.0.1:8000"
        ),
        session_expiration_seconds=3_600,
        session_cookie_secure=secure_cookie,
    )


@pytest.fixture
def auth_client(tmp_path: Path):
    settings = auth_settings(tmp_path / "auth-api.db")
    with TestClient(create_app(settings=settings), base_url=settings.app_origin) as client:
        yield client


def registration_payload(
    *,
    email: str = "Owner@Example.com",
    invite_code: str = INVITE_CODE,
) -> dict[str, str]:
    return {
        "invite_code": invite_code,
        "email": email,
        "password": PASSWORD,
        "display_name": "Owner",
        "timezone": "Asia/Shanghai",
    }


def register(client: TestClient, **overrides: str):
    payload = registration_payload(**overrides)
    return client.post("/api/auth/register", json=payload)


def test_successful_register_sets_session_and_returns_public_user(auth_client):
    response = register(auth_client)

    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"
    assert response.json()["display_name"] == "Owner"
    assert "password_hash" not in response.json()
    assert "session_token" not in response.json()
    assert auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    assert auth_client.cookies.get(CSRF_COOKIE_NAME)

    session_token = auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    session = auth_client.app.state.auth_repository.get_session_by_token_hash(
        hash_session_token(session_token)
    )
    assert session is not None
    assert session.user_id == response.json()["id"]
    assert session.token_hash != session_token
    assert session.csrf_token_hash != auth_client.cookies.get(CSRF_COOKIE_NAME)


def test_registration_rejects_wrong_invite(auth_client):
    response = register(auth_client, invite_code="wrong invite")

    assert response.status_code == 403
    assert auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME) is None


def test_registration_is_closed_by_default(tmp_path: Path):
    settings = auth_settings(
        tmp_path / "closed.db",
        registration_mode="closed",
    )
    with TestClient(create_app(settings=settings)) as client:
        response = register(client)

    assert response.status_code == 403
    assert response.json() == {"detail": "registration is closed"}


def test_duplicate_email_is_rejected_after_normalization(auth_client):
    assert register(auth_client).status_code == 201

    duplicate = register(auth_client, email="  OWNER@example.COM ")

    assert duplicate.status_code == 409


def test_login_success_creates_an_additional_session(auth_client):
    assert register(auth_client).status_code == 201
    auth_client.cookies.clear()

    response = auth_client.post(
        "/api/auth/login",
        json={"email": " OWNER@EXAMPLE.COM ", "password": PASSWORD},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "owner@example.com"
    assert auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    with auth_client.app.state.database.connection() as connection:
        session_count = connection.execute(
            "SELECT COUNT(*) FROM user_sessions"
        ).fetchone()[0]
    assert session_count == 2


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("owner@example.com", "incorrect password"),
        ("missing@example.com", PASSWORD),
    ],
)
def test_login_failure_does_not_create_session(auth_client, email, password):
    assert register(auth_client).status_code == 201
    auth_client.cookies.clear()

    response = auth_client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )

    assert response.status_code == 401
    assert auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME) is None


def test_me_with_valid_session(auth_client):
    registered = register(auth_client)

    response = auth_client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["id"] == registered.json()["id"]
    assert response.headers["cache-control"] == "no-store"


def test_me_without_session(auth_client):
    response = auth_client.get("/api/auth/me")

    assert response.status_code == 401


def test_logout_requires_csrf_and_revokes_only_current_session(auth_client):
    assert register(auth_client).status_code == 201
    first_session_token = auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    login = auth_client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    session_token = auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    csrf_token = auth_client.cookies.get(CSRF_COOKIE_NAME)
    assert session_token != first_session_token

    rejected = auth_client.post("/api/auth/logout")
    assert rejected.status_code == 403
    assert auth_client.get("/api/auth/me").status_code == 200

    response = auth_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 204
    assert auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME) is None
    assert auth_client.cookies.get(CSRF_COOKIE_NAME) is None
    session = auth_client.app.state.auth_repository.get_session_by_token_hash(
        hash_session_token(session_token)
    )
    assert session is not None
    assert session.revoked_time is not None
    assert auth_client.get("/api/auth/me").status_code == 401

    auth_client.cookies.set(LOCAL_SESSION_COOKIE_NAME, first_session_token)
    assert auth_client.get("/api/auth/me").status_code == 200


def test_wrong_csrf_token_is_rejected(auth_client):
    assert register(auth_client).status_code == 201

    response = auth_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": "wrong-token"},
    )

    assert response.status_code == 403
    assert auth_client.get("/api/auth/me").status_code == 200


def test_expired_session_is_rejected(auth_client):
    assert register(auth_client).status_code == 201
    session_token = auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    expired_time = (
        auth_client.app.state.auth_repository.get_session_by_token_hash(
            hash_session_token(session_token)
        ).created_time
        - timedelta(seconds=1)
    )
    with auth_client.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE user_sessions SET expires_time = ? WHERE token_hash = ?",
            (expired_time.isoformat(), hash_session_token(session_token)),
        )

    assert auth_client.get("/api/auth/me").status_code == 401


def test_revoked_session_is_rejected(auth_client):
    assert register(auth_client).status_code == 201
    session_token = auth_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    session = auth_client.app.state.auth_repository.get_session_by_token_hash(
        hash_session_token(session_token)
    )
    assert auth_client.app.state.auth_repository.revoke_session(session.id)

    assert auth_client.get("/api/auth/me").status_code == 401


def test_disabled_user_session_and_login_are_rejected(auth_client):
    registered = register(auth_client)
    with auth_client.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET status = 'disabled' WHERE id = ?",
            (registered.json()["id"],),
        )

    assert auth_client.get("/api/auth/me").status_code == 401
    auth_client.cookies.clear()
    login = auth_client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD},
    )
    assert login.status_code == 401


def test_production_cookie_attributes(tmp_path: Path):
    settings = auth_settings(tmp_path / "secure.db", secure_cookie=True)
    with TestClient(
        create_app(settings=settings), base_url="https://selfechoai.com"
    ) as client:
        response = register(client)
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )

    session_header = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    csrf_header = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{CSRF_COOKIE_NAME}=")
    )
    assert "Secure" in session_header
    assert "HttpOnly" in session_header
    assert "SameSite=lax" in session_header
    assert "Path=/" in session_header
    assert "Domain=" not in session_header
    assert "HttpOnly" not in csrf_header
    assert "Secure" in csrf_header

    logout_session_header = next(
        value
        for value in logout.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "Max-Age=0" in logout_session_header
    assert "Secure" in logout_session_header


def test_local_http_cookie_attributes_and_logout_match(tmp_path: Path):
    settings = auth_settings(tmp_path / "local.db", secure_cookie=False)
    with TestClient(create_app(settings=settings), base_url=settings.app_origin) as client:
        response = register(client)
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )

    session_header = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{LOCAL_SESSION_COOKIE_NAME}=")
    )
    assert not session_header.startswith("__Host-")
    assert "Secure" not in session_header
    assert "HttpOnly" in session_header
    assert "SameSite=lax" in session_header
    assert "Path=/" in session_header
    assert "Domain=" not in session_header

    logout_session_header = next(
        value
        for value in logout.headers.get_list("set-cookie")
        if value.startswith(f"{LOCAL_SESSION_COOKIE_NAME}=")
    )
    assert "Max-Age=0" in logout_session_header
    assert "Secure" not in logout_session_header


def test_expected_local_browser_origin_is_allowed(auth_client):
    response = auth_client.post(
        "/api/auth/register",
        json=registration_payload(),
        headers={"Origin": "http://127.0.0.1:8000"},
    )

    assert response.status_code == 201


def test_unexpected_browser_origin_is_rejected(auth_client):
    response = auth_client.post(
        "/api/auth/register",
        json=registration_payload(),
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403
