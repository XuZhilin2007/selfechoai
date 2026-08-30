from __future__ import annotations

from io import StringIO

import pytest

from app.auth import AuthenticationService
from app.auth_repository import AuthRepository
from app.bootstrap import bootstrap_first_user
from app.config import Settings
from app.database import Database


PASSWORD = "bootstrap test password"


def run_bootstrap(
    settings: Settings,
    *,
    answers: list[str],
    passwords: list[str],
) -> tuple[int, str, str]:
    answer_values = iter(answers)
    password_values = iter(passwords)
    stdout = StringIO()
    stderr = StringIO()
    result = bootstrap_first_user(
        settings,
        input_reader=lambda prompt: next(answer_values),
        password_reader=lambda prompt: next(password_values),
        stdout=stdout,
        stderr=stderr,
    )
    return result, stdout.getvalue(), stderr.getvalue()


def user_count(settings: Settings) -> int:
    database = Database(settings.database_path)
    database.initialize()
    return AuthRepository(database).count_users()


def test_zero_user_database_bootstraps_login_ready_user(tmp_path):
    settings = Settings(database_path=tmp_path / "bootstrap.db")

    result, stdout, stderr = run_bootstrap(
        settings,
        answers=[" Owner@Example.com ", "Owner", "Asia/Shanghai"],
        passwords=[PASSWORD, PASSWORD],
    )

    assert result == 0
    assert stderr == ""
    assert "First user created successfully" in stdout
    assert PASSWORD not in stdout
    assert "$argon2id$" not in stdout

    database = Database(settings.database_path)
    repository = AuthRepository(database)
    assert repository.count_users() == 1
    service = AuthenticationService(
        repository,
        registration_mode="closed",
        invite_code_hash="",
        session_expiration_seconds=3_600,
    )
    authenticated = service.login(
        email="owner@example.com",
        password=PASSWORD,
    )
    assert authenticated.user.email == "owner@example.com"
    assert authenticated.user.default_reminder_time == "09:00"


def test_existing_user_causes_bootstrap_refusal_without_prompting(tmp_path):
    settings = Settings(database_path=tmp_path / "existing.db")
    first_result, _, _ = run_bootstrap(
        settings,
        answers=["first@example.com", "First", "Asia/Shanghai"],
        passwords=[PASSWORD, PASSWORD],
    )
    assert first_result == 0

    stdout = StringIO()
    stderr = StringIO()
    result = bootstrap_first_user(
        settings,
        input_reader=lambda prompt: pytest.fail("existing-user bootstrap prompted"),
        password_reader=lambda prompt: pytest.fail("existing-user bootstrap prompted"),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 1
    assert "already contains a user" in stderr.getvalue()
    assert user_count(settings) == 1


def test_invalid_password_is_rejected_without_creating_user(tmp_path):
    settings = Settings(database_path=tmp_path / "invalid-password.db")
    invalid_password = "too-short"

    result, stdout, stderr = run_bootstrap(
        settings,
        answers=["owner@example.com", "Owner", "Asia/Shanghai"],
        passwords=[invalid_password, invalid_password],
    )

    assert result == 1
    assert "Invalid password" in stderr
    assert invalid_password not in stdout
    assert invalid_password not in stderr
    assert "$argon2id$" not in stdout + stderr
    assert user_count(settings) == 0


def test_password_confirmation_mismatch_does_not_create_user(tmp_path):
    settings = Settings(database_path=tmp_path / "mismatch.db")
    first_password = "first bootstrap password"
    second_password = "different bootstrap password"

    result, stdout, stderr = run_bootstrap(
        settings,
        answers=["owner@example.com", "Owner", "Asia/Shanghai"],
        passwords=[first_password, second_password],
    )

    assert result == 1
    assert "confirmation does not match" in stderr
    assert first_password not in stdout + stderr
    assert second_password not in stdout + stderr
    assert user_count(settings) == 0


def test_invalid_email_is_rejected_without_creating_user(tmp_path):
    settings = Settings(database_path=tmp_path / "invalid-email.db")

    result, _, stderr = run_bootstrap(
        settings,
        answers=["not-an-email", "Owner", "Asia/Shanghai"],
        passwords=[PASSWORD, PASSWORD],
    )

    assert result == 1
    assert "Invalid email" in stderr
    assert user_count(settings) == 0
