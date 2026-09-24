from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.services.tencent_ses import TencentSesEmailSender


ROOT = Path(__file__).resolve().parents[1]

# Account-specific SES values (credentials, sender identity, template IDs,
# verification pepper) must never carry a non-empty default anywhere in the
# Community Edition: the operator provisions their own Tencent account.
EMPTY_ENV_DEFAULTS = (
    "TENCENTCLOUD_SECRET_ID=",
    "TENCENTCLOUD_SECRET_KEY=",
    "TENCENT_SES_FROM_EMAIL_ADDRESS=",
    "TENCENT_SES_VERIFICATION_TEMPLATE_ID=",
    "TENCENT_SES_REMINDER_TEMPLATE_ID=",
    "TENCENT_SES_TEST_TEMPLATE_ID=",
    "EMAIL_VERIFICATION_CODE_PEPPER=",
)

EMPTY_CONFIG_DEFAULTS = (
    "tencentcloud_secret_id: SecretStr = SecretStr(\"\"",
    "tencentcloud_secret_key: SecretStr = SecretStr(\"\"",
    "tencent_ses_from_email_address: str = \"\"",
    "tencent_ses_verification_template_id: int | None = None",
    "tencent_ses_reminder_template_id: int | None = None",
    "tencent_ses_test_template_id: int | None = None",
    "email_verification_code_pepper: SecretStr = SecretStr(\"\"",
)

_EMAIL_LITERAL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Template IDs are provider-assigned account-specific integers; the provider
# adapter must source them from settings instead of hardcoding them.
_FIVE_DIGIT_LITERAL = re.compile(r"\b\d{5}\b")


def test_normal_email_boundary_has_no_item_specific_arguments_or_queries():
    parameters = inspect.signature(
        TencentSesEmailSender.send_reminder
    ).parameters
    assert "app_url" in parameters
    assert "title" not in parameters
    assert "item_id" not in parameters
    assert "reminder_id" not in parameters

    repository_source = (ROOT / "app" / "email_repository.py").read_text(
        encoding="utf-8"
    )
    claim_section = repository_source.split(
        "def claim_next_delivery", 1
    )[1].split("def claim_next_status_check", 1)[0]
    assert "items.title" not in claim_section
    assert "original_text" not in claim_section

    service_source = (ROOT / "app" / "services" / "email_reminders.py").read_text(
        encoding="utf-8"
    )
    normal_send = service_source.split("self.sender.send_reminder(", 1)[1].split(
        ")", 1
    )[0]
    assert "app_url=" in normal_send
    assert "title" not in normal_send
    assert "item_id" not in normal_send
    assert "reminder_id" not in normal_send


def test_community_email_configuration_ships_only_empty_account_defaults():
    environment_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in EMPTY_ENV_DEFAULTS:
        assert line in environment_example

    config_source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    for default in EMPTY_CONFIG_DEFAULTS:
        assert default in config_source


def test_provider_adapter_sources_account_values_from_settings_only():
    source = (ROOT / "app" / "services" / "tencent_ses.py").read_text(
        encoding="utf-8"
    )
    # Sender and template IDs flow exclusively from operator settings, with a
    # fail-closed helper when a required value is missing.
    assert "self.settings.tencent_ses_from_email_address" in source
    assert "self.settings.tencent_ses_verification_template_id" in source
    assert "self.settings.tencent_ses_reminder_template_id" in source
    assert "self.settings.tencent_ses_test_template_id" in source
    assert "_required_template_id" in source
    assert "EMAIL_REMINDER_PROVIDER_ENABLED" not in source
    # No email-address literal and no hardcoded provider-assigned template ID.
    assert _EMAIL_LITERAL.search(source) is None
    assert _FIVE_DIGIT_LITERAL.search(source) is None
