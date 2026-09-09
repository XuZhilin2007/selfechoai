from __future__ import annotations

import inspect
from pathlib import Path

from app.services.tencent_ses import TencentSesEmailSender


ROOT = Path(__file__).resolve().parents[1]


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


def test_community_email_configuration_contains_no_private_operator_defaults():
    inspected = "\n".join(
        [
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            (ROOT / "app" / "config.py").read_text(encoding="utf-8"),
            (ROOT / "app" / "services" / "tencent_ses.py").read_text(
                encoding="utf-8"
            ),
        ]
    )
    for forbidden in (
        "591" + "14",
        "591" + "15",
        "591" + "97",
        "reminder@notify." + "selfechoai" + ".com",
    ):
        assert forbidden not in inspected
