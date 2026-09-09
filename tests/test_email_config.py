from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


EMAIL_ENV_NAMES = (
    "EMAIL_REMINDER_PROVIDER_ENABLED",
    "TENCENT_SES_REGION",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "TENCENT_SES_FROM_EMAIL_ADDRESS",
    "TENCENT_SES_VERIFICATION_TEMPLATE_ID",
    "TENCENT_SES_REMINDER_TEMPLATE_ID",
    "TENCENT_SES_TEST_TEMPLATE_ID",
    "TENCENT_SES_TIMEOUT_SECONDS",
    "EMAIL_VERIFICATION_CODE_PEPPER",
)

VALID_PROVIDER_ENV = """
EMAIL_REMINDER_PROVIDER_ENABLED=true
TENCENT_SES_REGION=ap-shanghai
TENCENTCLOUD_SECRET_ID=community-test-id
TENCENTCLOUD_SECRET_KEY=community-test-key
TENCENT_SES_FROM_EMAIL_ADDRESS=SelfEcho Community <reminder@community.example>
TENCENT_SES_VERIFICATION_TEMPLATE_ID=101
TENCENT_SES_REMINDER_TEMPLATE_ID=102
EMAIL_VERIFICATION_CODE_PEPPER=community-test-pepper
"""


def load(tmp_path: Path, monkeypatch, content: str) -> Settings:
    for name in EMAIL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return Settings.from_environment(env_file)


def test_email_provider_is_disabled_by_default_without_operator_values(
    tmp_path, monkeypatch
):
    settings = load(tmp_path, monkeypatch, "")
    assert settings.email_reminder_provider_enabled is False
    assert settings.tencent_ses_region == "ap-guangzhou"
    assert settings.tencent_ses_verification_template_id is None
    assert settings.tencent_ses_reminder_template_id is None
    assert settings.tencent_ses_test_template_id is None
    assert settings.tencent_ses_from_email_address == ""


def test_disabled_provider_does_not_parse_unused_malformed_email_values(
    tmp_path, monkeypatch
):
    settings = load(
        tmp_path,
        monkeypatch,
        """
EMAIL_REMINDER_PROVIDER_ENABLED=false
TENCENT_SES_REGION=not a region
TENCENT_SES_VERIFICATION_TEMPLATE_ID=not-an-integer
TENCENT_SES_REMINDER_TEMPLATE_ID=0
TENCENT_SES_TIMEOUT_SECONDS=not-a-number
""",
    )
    assert settings.email_reminder_provider_enabled is False
    assert settings.tencent_ses_verification_template_id is None
    assert settings.tencent_ses_reminder_template_id is None
    assert settings.tencent_ses_timeout_seconds == 10.0


def test_enabled_provider_requires_all_security_and_transport_settings(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError, match="TENCENT_SES_FROM_EMAIL_ADDRESS"):
        load(tmp_path, monkeypatch, "EMAIL_REMINDER_PROVIDER_ENABLED=true\n")


def test_enabled_provider_loads_configurable_region_and_redacts_secrets(
    tmp_path, monkeypatch
):
    settings = load(
        tmp_path,
        monkeypatch,
        VALID_PROVIDER_ENV + "TENCENT_SES_TEST_TEMPLATE_ID=103\n",
    )
    assert settings.email_reminder_provider_enabled is True
    assert settings.tencent_ses_region == "ap-shanghai"
    assert settings.tencent_ses_verification_template_id == 101
    assert settings.tencent_ses_reminder_template_id == 102
    assert settings.tencent_ses_test_template_id == 103
    assert settings.tencentcloud_secret_key.get_secret_value() == "community-test-key"
    rendered = repr(settings)
    assert "community-test-id" not in rendered
    assert "community-test-key" not in rendered
    assert "community-test-pepper" not in rendered


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("TENCENT_SES_REGION=not a region", "region name"),
        ("TENCENT_SES_REMINDER_TEMPLATE_ID=0", "positive integer"),
        ("TENCENT_SES_TIMEOUT_SECONDS=0", "greater than 0"),
        ("TENCENT_SES_TIMEOUT_SECONDS=61", "at most 60"),
    ],
)
def test_invalid_enabled_email_provider_values_are_rejected(
    tmp_path, monkeypatch, line, message
):
    lines = [
        existing
        for existing in VALID_PROVIDER_ENV.strip().splitlines()
        if existing.split("=", 1)[0] != line.split("=", 1)[0]
    ]
    with pytest.raises(ValueError, match=message):
        load(tmp_path, monkeypatch, "\n".join([*lines, line, ""]))
