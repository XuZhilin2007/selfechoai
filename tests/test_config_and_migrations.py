from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.database import CURRENT_SCHEMA_VERSION, Database, DatabaseVersionError


def test_settings_use_selfecho_database_name_for_new_installations(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_DATABASE_PATH", raising=False)
    for name in (
        "AUTH_REGISTRATION_MODE",
        "AUTH_INVITE_CODE_HASH",
        "APP_ORIGIN",
        "AUTH_SESSION_EXPIRATION_SECONDS",
        "AUTH_COOKIE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.database_path == Path("data/selfecho.db")
    assert settings.registration_mode == "closed"
    assert settings.invite_code_hash == ""
    assert settings.app_origin == "http://127.0.0.1:8000"
    assert settings.session_cookie_secure is False


def test_settings_preserve_existing_pre_rename_database(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    legacy_path = tmp_path / "data" / "personal_ai_inbox.db"
    legacy_path.parent.mkdir()
    legacy_path.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_DATABASE_PATH", raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.database_path == Path("data/personal_ai_inbox.db")


def test_settings_load_project_env_and_process_environment_wins(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AI_PROVIDER=openai",
                "OPENAI_API_KEY=from-file",
                "AI_MODEL=file-model",
                "AI_TIMEOUT_SECONDS=12",
                "AI_DEBUG_OUTPUT=true",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_MODEL", "process-model")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.ai_provider == "openai"
    assert settings.ai_api_key == "from-file"
    assert settings.ai_model == "process-model"
    assert settings.ai_timeout_seconds == 12
    assert settings.ai_debug_output is True


def test_settings_load_deepseek_env_with_official_defaults(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AI_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=deepseek-secret",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "AI_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.ai_provider == "deepseek"
    assert settings.deepseek_api_key == "deepseek-secret"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_api_url == "https://api.deepseek.com"
    assert settings.ai_debug_output is False


def test_settings_load_authentication_environment(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    invite_hash = "a" * 64
    env_file.write_text(
        "\n".join(
            [
                "AUTH_REGISTRATION_MODE=invite",
                f"AUTH_INVITE_CODE_HASH={invite_hash}",
                "APP_ORIGIN=http://localhost:8000/",
                "AUTH_SESSION_EXPIRATION_SECONDS=7200",
                "AUTH_COOKIE_SECURE=false",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "AUTH_REGISTRATION_MODE",
        "AUTH_INVITE_CODE_HASH",
        "APP_ORIGIN",
        "AUTH_SESSION_EXPIRATION_SECONDS",
        "AUTH_COOKIE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(env_file=env_file)

    assert settings.registration_mode == "invite"
    assert settings.invite_code_hash == invite_hash
    assert settings.app_origin == "http://localhost:8000"
    assert settings.session_expiration_seconds == 7200
    assert settings.session_cookie_secure is False


def test_reminder_worker_settings_are_safe_by_default_and_load_from_env(
    tmp_path: Path,
    monkeypatch,
):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    for name in (
        "REMINDER_WORKER_ENABLED",
        "REMINDER_POLL_INTERVAL_SECONDS",
        "REMINDER_BATCH_SIZE",
        "REMINDER_SENDING_STALE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings.from_environment(env_file=empty_env)

    assert defaults.reminder_worker_enabled is False
    assert defaults.reminder_poll_interval_seconds == 30
    assert defaults.reminder_batch_size == 100
    assert defaults.reminder_sending_stale_seconds == 300

    configured_env = tmp_path / "worker.env"
    configured_env.write_text(
        "\n".join(
            [
                "REMINDER_WORKER_ENABLED=true",
                "REMINDER_POLL_INTERVAL_SECONDS=12.5",
                "REMINDER_BATCH_SIZE=25",
                "REMINDER_SENDING_STALE_SECONDS=600",
            ]
        ),
        encoding="utf-8",
    )
    configured = Settings.from_environment(env_file=configured_env)

    assert configured.reminder_worker_enabled is True
    assert configured.web_push_enabled is False
    assert configured.reminder_poll_interval_seconds == 12.5
    assert configured.reminder_batch_size == 25
    assert configured.reminder_sending_stale_seconds == 600


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "reminder_poll_interval_seconds",
            float("nan"),
            "REMINDER_POLL_INTERVAL_SECONDS",
        ),
        ("reminder_poll_interval_seconds", 0, "REMINDER_POLL_INTERVAL_SECONDS"),
        (
            "reminder_poll_interval_seconds",
            3_601,
            "REMINDER_POLL_INTERVAL_SECONDS",
        ),
        ("reminder_batch_size", 0, "REMINDER_BATCH_SIZE"),
        ("reminder_batch_size", 1_001, "REMINDER_BATCH_SIZE"),
        ("reminder_sending_stale_seconds", 0, "REMINDER_SENDING_STALE_SECONDS"),
        (
            "reminder_sending_stale_seconds",
            86_401,
            "REMINDER_SENDING_STALE_SECONDS",
        ),
    ],
)
def test_reminder_worker_settings_are_positive_finite_and_bounded(
    tmp_path: Path,
    field: str,
    value: float | int,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        Settings(database_path=tmp_path / "invalid-worker.db", **{field: value})


def test_https_origin_requires_secure_cookie(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ORIGIN=https://community.example\nAUTH_COOKIE_SECURE=false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_ORIGIN", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)

    with pytest.raises(
        ValueError,
        match="AUTH_COOKIE_SECURE must be true when APP_ORIGIN uses HTTPS",
    ):
        Settings.from_environment(env_file=env_file)


def test_new_database_initializes_directly_to_current_v6(tmp_path: Path):
    database_path = tmp_path / "new.db"

    Database(database_path).initialize()

    connection = sqlite3.connect(database_path)
    tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    item_columns = {
        row[1]: row[3]
        for row in connection.execute("PRAGMA table_info(personal_items)")
    }
    input_columns = {
        row[1]: row[3]
        for row in connection.execute("PRAGMA table_info(item_inputs)")
    }
    user_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(users)")
    }
    session_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(user_sessions)")
    }
    indexes = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()

    assert {
        "users",
        "user_sessions",
        "personal_items",
        "item_inputs",
        "reminders",
        "push_subscriptions",
        "reminder_deliveries",
        "email_reminder_settings",
        "email_verification_challenges",
        "reminder_email_deliveries",
    } <= tables
    assert item_columns["user_id"] == 1
    assert "reminder_prompt_dismissed_at" in item_columns
    assert input_columns["user_id"] == 1
    assert {
        "id",
        "email",
        "password_hash",
        "display_name",
        "timezone",
        "default_reminder_time",
        "status",
        "password_changed_time",
        "created_time",
        "updated_time",
    } <= user_columns
    assert {
        "id",
        "user_id",
        "token_hash",
        "csrf_token_hash",
        "created_time",
        "last_seen_time",
        "expires_time",
        "revoked_time",
        "user_agent",
    } <= session_columns
    assert {
        "idx_user_sessions_id_user",
        "idx_reminders_status_remind",
        "idx_reminders_user_status_remind",
        "idx_reminders_user_item_created",
        "uq_reminders_user_item_active",
        "idx_push_subscriptions_user_status",
        "uq_push_subscriptions_endpoint",
        "idx_reminder_deliveries_user_status",
        "uq_reminder_deliveries_pair",
    } <= indexes
    assert version == CURRENT_SCHEMA_VERSION == 6
    assert foreign_key_errors == []
    assert integrity == "ok"


@pytest.mark.parametrize("old_version", [1, 2])
def test_existing_old_database_requires_explicit_migration(
    tmp_path: Path, old_version: int
):
    database_path = tmp_path / f"v{old_version}.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        f"""
        CREATE TABLE personal_items (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            importance TEXT NOT NULL,
            urgency TEXT NOT NULL,
            deadline TEXT,
            estimated_time INTEGER,
            status TEXT NOT NULL,
            next_action TEXT,
            extra_information TEXT,
            created_time TEXT NOT NULL,
            updated_time TEXT NOT NULL
        );
        CREATE TABLE item_inputs (
            id INTEGER PRIMARY KEY,
            item_id INTEGER,
            original_text TEXT NOT NULL,
            input_method TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            created_time TEXT NOT NULL
        );
        PRAGMA user_version = {old_version};
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(DatabaseVersionError, match="explicit migration"):
        Database(database_path).initialize()
