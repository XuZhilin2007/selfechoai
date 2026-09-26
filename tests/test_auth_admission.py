from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.auth_admission import AdmissionDenied, AuthAdmissionLimiter
from app.auth import hash_invite_code
from app.config import Settings
from app.main import create_app


PASSWORD = "a strong test password"
INVITE_CODE = "auth-admission-invite"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def settings(tmp_path: Path, registration_mode: str = "invite", **limits) -> Settings:
    return replace(
        Settings(
            database_path=tmp_path / "auth-admission.db",
            registration_mode=registration_mode,
            invite_code_hash=(
                hash_invite_code(INVITE_CODE)
                if registration_mode == "invite"
                else ""
            ),
            app_origin="http://testserver",
            session_cookie_secure=False,
        ),
        **limits,
    )


def register(client: TestClient, email: str = "owner@example.com"):
    return client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE_CODE,
            "email": email,
            "password": PASSWORD,
            "display_name": "Owner",
            "timezone": "Asia/Shanghai",
        },
    )


def login(client: TestClient, email: str, password: str = "wrong password"):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )


def counts(client: TestClient) -> tuple[int, int]:
    with client.app.state.database.connection() as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0],
        )


def assert_limited(response, retry_after: str = "60") -> None:
    assert response.status_code == 429
    assert response.json() == {"detail": "too many attempts; try again later"}
    assert response.headers["retry-after"] == retry_after
    assert response.headers["cache-control"] == "no-store"


def test_registration_source_counts_duplicate_and_rejects_before_hash_or_write(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(settings(tmp_path, auth_registration_source_limit=2))
    with TestClient(app, client=("198.51.100.10", 50000)) as client:
        assert register(client).status_code == 201
        assert register(client, " OWNER@example.COM ").status_code == 409
        before = counts(client)
        monkeypatch.setattr(auth_module, "hash_password", lambda _: pytest.fail("hashed"))
        denied = register(client, "new@example.com")
        assert_limited(denied)
        assert counts(client) == before == (1, 1)


def test_registration_global_ceiling_is_independent_of_source_limit(
    tmp_path: Path,
) -> None:
    app = create_app(settings(
        tmp_path,
        auth_registration_source_limit=10,
        auth_registration_global_limit=2,
    ))
    with TestClient(app) as client:
        assert register(client, "one@example.com").status_code == 201
        assert register(client, "two@example.com").status_code == 201
        assert_limited(register(client, "three@example.com"))
        assert counts(client) == (2, 2)


def test_registration_source_windows_are_independent(tmp_path: Path) -> None:
    limiter = AuthAdmissionLimiter(settings(
        tmp_path,
        auth_registration_source_limit=1,
        auth_registration_global_limit=10,
    ))
    limiter.admit_registration("198.51.100.1")
    limiter.admit_registration("198.51.100.2")
    with pytest.raises(AdmissionDenied):
        limiter.admit_registration("198.51.100.1")


def test_login_source_counts_failures_and_rejects_before_dummy_or_real_argon2(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(settings(tmp_path, auth_login_source_limit=2))
    with TestClient(app, client=("198.51.100.11", 50000)) as client:
        assert register(client).status_code == 201
        assert login(client, "owner@example.com").status_code == 401
        assert login(client, "missing@example.com").status_code == 401
        before = counts(client)
        monkeypatch.setattr(auth_module, "verify_password", lambda *_: pytest.fail("verified"))
        assert_limited(login(client, "owner@example.com", PASSWORD))
        assert counts(client) == before == (1, 1)


@pytest.mark.parametrize("account_exists", [True, False])
def test_normalized_account_limit_does_not_depend_on_account_existence(
    tmp_path: Path, account_exists: bool
) -> None:
    app = create_app(settings(
        tmp_path,
        auth_login_source_limit=10,
        auth_login_account_limit=2,
        auth_login_global_limit=10,
    ))
    with TestClient(app) as client:
        if account_exists:
            assert register(client).status_code == 201
        base = "owner@example.com" if account_exists else "missing@example.com"
        assert login(client, base).status_code == 401
        assert login(client, f"  {base.upper()}  ").status_code == 401
        assert_limited(login(client, base))


def test_login_global_ceiling_covers_unique_identifiers(tmp_path: Path) -> None:
    app = create_app(settings(
        tmp_path,
        auth_login_source_limit=10,
        auth_login_account_limit=10,
        auth_login_global_limit=2,
    ))
    with TestClient(app) as client:
        assert login(client, "one@example.com").status_code == 401
        assert login(client, "two@example.com").status_code == 401
        assert_limited(login(client, "three@example.com"))


def test_window_expiry_recovers_without_sleep(tmp_path: Path) -> None:
    clock = Clock()
    app = create_app(
        settings(
            tmp_path,
            auth_registration_source_limit=1,
            auth_login_account_limit=1,
        ),
        auth_admission_clock=clock,
    )
    with TestClient(app) as client:
        assert register(client).status_code == 201
        assert_limited(register(client, "second@example.com"))
        assert login(client, "owner@example.com").status_code == 401
        assert_limited(login(client, "OWNER@EXAMPLE.COM"))
        clock.advance(60)
        assert register(client, "second@example.com").status_code == 201
        assert login(client, "owner@example.com").status_code == 401


def test_distinct_identifier_spray_cannot_grow_state_without_bound(
    tmp_path: Path,
) -> None:
    clock = Clock()
    limiter = AuthAdmissionLimiter(
        settings(
            tmp_path,
            auth_admission_max_keys=2,
            auth_login_source_limit=20,
            auth_login_account_limit=20,
            auth_login_global_limit=20,
        ),
        clock=clock,
    )
    limiter.admit_login("198.51.100.1", "one@example.com")
    limiter.admit_login("198.51.100.2", "two@example.com")
    with pytest.raises(AdmissionDenied) as denied:
        limiter.admit_login("198.51.100.3", "three@example.com")
    assert denied.value.retry_after == 60
    assert len(limiter._login_source.entries) == 2
    assert len(limiter._login_account.entries) == 2
    clock.advance(60)
    limiter.admit_login("198.51.100.3", "three@example.com")
    assert len(limiter._login_source.entries) == 1
    assert len(limiter._login_account.entries) == 1


def test_direct_asgi_scope_does_not_use_forged_forwarding_headers(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path, auth_registration_source_limit=1))
    with TestClient(app, client=("198.51.100.12", 50000)) as client:
        first = register(client, "one@example.com")
        second = client.post(
            "/api/auth/register",
            json={
                "invite_code": INVITE_CODE,
                "email": "two@example.com",
                "password": PASSWORD,
                "display_name": "Owner",
                "timezone": "Asia/Shanghai",
            },
            headers={
                "X-Forwarded-For": "203.0.113.10",
                "X-Real-IP": "203.0.113.11",
            },
        )
        assert first.status_code == 201
        assert_limited(second)


def test_closed_registration_is_limited_but_existing_login_works(
    tmp_path: Path,
) -> None:
    invite_settings = settings(tmp_path)
    with TestClient(create_app(invite_settings)) as client:
        assert register(client).status_code == 201
    closed_settings = replace(
        invite_settings,
        registration_mode="closed",
        invite_code_hash="",
        auth_registration_source_limit=1,
    )
    with TestClient(create_app(closed_settings)) as client:
        assert register(client, "new@example.com").status_code == 403
        assert_limited(register(client, "another@example.com"))
        assert login(client, "owner@example.com", PASSWORD).status_code == 200


@pytest.mark.parametrize("name", [
    "AUTH_REGISTRATION_WINDOW_SECONDS",
    "AUTH_REGISTRATION_SOURCE_LIMIT",
    "AUTH_REGISTRATION_GLOBAL_LIMIT",
    "AUTH_LOGIN_WINDOW_SECONDS",
    "AUTH_LOGIN_SOURCE_LIMIT",
    "AUTH_LOGIN_ACCOUNT_LIMIT",
    "AUTH_LOGIN_GLOBAL_LIMIT",
    "AUTH_ADMISSION_MAX_KEYS",
])
@pytest.mark.parametrize("value", ["0", "invalid"])
def test_auth_admission_configuration_fails_clearly(
    monkeypatch, tmp_path: Path, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_environment(tmp_path / "empty.env")
