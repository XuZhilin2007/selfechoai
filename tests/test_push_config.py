from __future__ import annotations

import base64
import importlib.metadata
import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings


def enabled_settings(
    database_path: Path,
    vapid_key_pair: tuple[str, str],
    **overrides: object,
) -> Settings:
    public_key, private_key = vapid_key_pair
    values: dict[str, object] = {
        "database_path": database_path,
        "web_push_enabled": True,
        "web_push_vapid_public_key": public_key,
        "web_push_vapid_private_key": SecretStr(private_key),
        "web_push_vapid_subject": "mailto:admin@example.com",
    }
    values.update(overrides)
    return Settings(**values)


def test_disabled_web_push_starts_without_vapid_configuration(tmp_path: Path):
    settings = Settings(database_path=tmp_path / "disabled.db")

    assert settings.web_push_enabled is False
    assert settings.web_push_vapid_public_key == ""
    assert settings.web_push_vapid_private_key.get_secret_value() == ""


def test_enabled_web_push_accepts_matching_synthetic_keys(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    settings = enabled_settings(tmp_path / "enabled.db", vapid_key_pair)

    assert settings.web_push_enabled is True
    assert settings.web_push_timeout_seconds == 10.0


@pytest.mark.parametrize(
    "subject",
    [
        "",
        "http://example.com",
        "mailto:",
        "mailto:user@example.com?secret=value",
        "https://user:password@example.com",
        "https://example.com:8443",
    ],
)
def test_enabled_web_push_rejects_invalid_vapid_subject(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subject: str,
):
    with pytest.raises(ValueError, match="WEB_PUSH_VAPID_SUBJECT"):
        enabled_settings(
            tmp_path / "subject.db",
            vapid_key_pair,
            web_push_vapid_subject=subject,
        )


def test_enabled_web_push_rejects_invalid_public_key(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    with pytest.raises(ValueError, match="WEB_PUSH_VAPID_PUBLIC_KEY"):
        enabled_settings(
            tmp_path / "public.db",
            vapid_key_pair,
            web_push_vapid_public_key="not-a-public-key",
        )


def test_enabled_web_push_rejects_invalid_private_key_without_echoing_it(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    secret_value = "synthetic-private-value-that-must-not-appear"
    with pytest.raises(ValueError) as error:
        enabled_settings(
            tmp_path / "private.db",
            vapid_key_pair,
            web_push_vapid_private_key=SecretStr(secret_value),
        )

    assert "WEB_PUSH_VAPID_PRIVATE_KEY" in str(error.value)
    assert secret_value not in str(error.value)


def test_enabled_web_push_rejects_a_valid_but_mismatched_private_key(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    mismatched_private = base64.urlsafe_b64encode((1).to_bytes(32, "big")).rstrip(
        b"="
    ).decode()

    with pytest.raises(ValueError, match="must match"):
        enabled_settings(
            tmp_path / "mismatch.db",
            vapid_key_pair,
            web_push_vapid_private_key=SecretStr(mismatched_private),
        )


def test_partial_enabled_configuration_fails_explicitly(tmp_path: Path):
    with pytest.raises(ValueError):
        Settings(database_path=tmp_path / "partial.db", web_push_enabled=True)


@pytest.mark.parametrize("timeout", [0.0, -1.0, 31.0, float("inf"), float("nan")])
def test_enabled_web_push_requires_a_finite_bounded_timeout(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    timeout: float,
):
    with pytest.raises(ValueError, match="WEB_PUSH_TIMEOUT_SECONDS"):
        enabled_settings(
            tmp_path / "timeout.db",
            vapid_key_pair,
            web_push_timeout_seconds=timeout,
        )


def test_test_send_cannot_be_enabled_when_web_push_is_disabled(tmp_path: Path):
    with pytest.raises(ValueError, match="WEB_PUSH_TEST_SEND_ENABLED"):
        Settings(
            database_path=tmp_path / "test-send.db",
            web_push_test_send_enabled=True,
        )


def test_private_vapid_key_is_redacted_in_settings_repr(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    settings = enabled_settings(tmp_path / "redaction.db", vapid_key_pair)
    private_key = vapid_key_pair[1]

    assert private_key not in repr(settings)
    assert "**********" in repr(settings)


def test_project_declares_frozen_pywebpush_and_retains_tzdata():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "pywebpush==2.4.0" in dependencies
    assert "tzdata>=2025.2" in dependencies
    assert project["project"]["version"] == "0.3.0"
    assert importlib.metadata.version("pywebpush") == "2.4.0"
