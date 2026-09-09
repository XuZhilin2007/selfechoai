from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_account_email_ui_is_local_and_does_not_change_deep_link_security():
    frontend = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    service_worker = (ROOT / "app" / "static" / "service-worker.js").read_text(
        encoding="utf-8"
    )
    assert "Email Reminder" in frontend
    assert "此设备通知" in frontend
    assert "/api/email-reminders/settings" in frontend
    assert "safeLoginDestination" in frontend
    assert 'DEFAULT_NOTIFICATION_TARGET = "/dashboard"' in service_worker
    assert "safeNotificationTargetPath" in service_worker
    assert "verification code" not in service_worker.casefold()


def test_email_ui_has_no_future_channel_placeholders_or_received_claim():
    frontend = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    email_section = frontend[
        frontend.index("function loadEmailReminderSettings") :
        frontend.index("function safeLoginDestination")
    ]
    assert "测试邮件已提交" not in email_section
    assert "你已收到" not in email_section
    assert "WeChat" not in email_section
    assert "SMS" not in email_section
    assert "Native Push" not in email_section
