from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.schemas import AIExtraction, PersonalItemPublic
from app.services.ai import AIService
from app.services.tencent_ses import EmailSendOutcome, EmailSendResult
from tests.email_fakes import RecordingEmailSender


INVITE = "email-api-invite"
PASSWORD = "email api password"


class NoopAIService(AIService):
    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        raise AssertionError("Email API tests must not invoke AI")


def make_client(
    tmp_path: Path,
    *,
    sender: RecordingEmailSender | None = None,
    provider_enabled: bool = True,
) -> tuple[TestClient, RecordingEmailSender]:
    sender = sender or RecordingEmailSender()
    settings = Settings(
        database_path=tmp_path / "email-api.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        email_reminder_provider_enabled=provider_enabled,
        tencentcloud_secret_id=SecretStr("community-test-id") if provider_enabled else SecretStr(""),
        tencentcloud_secret_key=SecretStr("community-test-key") if provider_enabled else SecretStr(""),
        tencent_ses_from_email_address=(
            "SelfEcho Community <reminder@community.example>"
            if provider_enabled
            else ""
        ),
        tencent_ses_verification_template_id=101 if provider_enabled else None,
        tencent_ses_reminder_template_id=102 if provider_enabled else None,
        email_verification_code_pepper=SecretStr("api-test-pepper"),
    )
    client = TestClient(
        create_app(
            settings=settings,
            ai_service=NoopAIService(),
            email_sender=sender,
        )
    )
    client.__enter__()
    return client, sender


def register(client: TestClient, email="login@example.com") -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE,
            "email": email,
            "password": PASSWORD,
            "display_name": "Email User",
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def test_settings_require_authentication_and_default_is_truthful(tmp_path):
    client, _sender = make_client(tmp_path)
    try:
        assert client.get("/api/email-reminders/settings").status_code == 401
        register(client)
        payload = client.get("/api/email-reminders/settings").json()
        assert payload == {
            "email_address": None,
            "verification_status": "pending",
            "verified_at": None,
            "enabled": False,
            "health_status": "healthy",
            "pause_reason": None,
            "effective_active": False,
            "provider_available": True,
            "test_email_available": False,
        }
    finally:
        client.__exit__(None, None, None)


def test_state_changes_require_csrf_and_reject_arbitrary_fields(tmp_path):
    client, _sender = make_client(tmp_path)
    try:
        register(client)
        assert client.put(
            "/api/email-reminders/address",
            json={"email_address": "reminder@example.com"},
        ).status_code == 403
        assert client.put(
            "/api/email-reminders/address",
            headers=csrf_headers(client),
            json={
                "email_address": "reminder@example.com",
                "destination": "attacker@example.com",
                "template_id": 1,
            },
        ).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_full_verification_and_enable_flow_never_returns_plaintext_code(tmp_path):
    client, sender = make_client(tmp_path)
    try:
        register(client)
        headers = csrf_headers(client)
        changed = client.put(
            "/api/email-reminders/address",
            headers=headers,
            json={"email_address": " Reminder@Example.COM "},
        )
        assert changed.status_code == 200
        assert changed.json()["email_address"] == "reminder@example.com"
        sent = client.post(
            "/api/email-reminders/verification/send", headers=headers
        )
        assert sent.status_code == 200
        assert "已提交发送" in sent.json()["message"]
        assert "code" not in sent.text.casefold()
        destination, code = sender.verification_calls[0]
        assert destination == "reminder@example.com"
        assert len(code) == 6 and code.isdigit()

        wrong = client.post(
            "/api/email-reminders/verification/confirm",
            headers=headers,
            json={"code": "999999" if code != "999999" else "888888"},
        )
        assert wrong.status_code == 409
        confirmed = client.post(
            "/api/email-reminders/verification/confirm",
            headers=headers,
            json={"code": code},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["settings"]["verification_status"] == "verified"
        enabled = client.put(
            "/api/email-reminders/enabled",
            headers=headers,
            json={"enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["effective_active"] is True
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("outcome", "persisted_status", "detail_fragment"),
    [
        (EmailSendOutcome.PERMANENT_FAILURE, "failed", "提交失败"),
        (EmailSendOutcome.AMBIGUOUS_FAILURE, "unknown", "结果不确定"),
    ],
)
def test_verification_provider_failure_is_truthful_and_persisted(
    tmp_path,
    outcome,
    persisted_status,
    detail_fragment,
):
    sender = RecordingEmailSender()
    sender.queue_send_results(
        EmailSendResult(outcome=outcome, error_code="provider-test-error")
    )
    client, _sender = make_client(tmp_path, sender=sender)
    try:
        register(client)
        headers = csrf_headers(client)
        client.put(
            "/api/email-reminders/address",
            headers=headers,
            json={"email_address": "verify-result@example.com"},
        )
        response = client.post(
            "/api/email-reminders/verification/send", headers=headers
        )
        assert response.status_code == 502
        assert detail_fragment in response.json()["detail"]
        assert sender.verification_calls[0][1] not in response.text
        with client.app.state.database.connection() as connection:
            challenge = connection.execute(
                "SELECT send_status, code_hmac FROM email_verification_challenges"
            ).fetchone()
        assert challenge["send_status"] == persisted_status
        assert len(challenge["code_hmac"]) == 64
        assert sender.verification_calls[0][1] != challenge["code_hmac"]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "outcome",
    [EmailSendOutcome.PERMANENT_FAILURE, EmailSendOutcome.AMBIGUOUS_FAILURE],
)
def test_failed_or_ambiguous_verification_sends_consume_account_quota(
    tmp_path,
    outcome,
):
    sender = RecordingEmailSender()
    sender.queue_send_results(
        EmailSendResult(outcome=outcome, error_code="provider-test-error")
    )
    client, _sender = make_client(tmp_path, sender=sender)
    try:
        register(client)
        headers = csrf_headers(client)
        for index in range(10):
            changed = client.put(
                "/api/email-reminders/address",
                headers=headers,
                json={"email_address": f"failed-{index}@example.com"},
            )
            assert changed.status_code == 200
            response = client.post(
                "/api/email-reminders/verification/send",
                headers=headers,
            )
            assert response.status_code == 502

        changed = client.put(
            "/api/email-reminders/address",
            headers=headers,
            json={"email_address": "blocked-eleventh@example.com"},
        )
        assert changed.status_code == 200
        blocked = client.post(
            "/api/email-reminders/verification/send",
            headers=headers,
        )
        assert blocked.status_code == 429
        assert len(sender.verification_calls) == 10
    finally:
        client.__exit__(None, None, None)


def test_email_settings_are_isolated_between_api_users(tmp_path):
    client, _sender = make_client(tmp_path)
    try:
        register(client, email="first@example.com")
        first_headers = csrf_headers(client)
        client.put(
            "/api/email-reminders/address",
            headers=first_headers,
            json={"email_address": "first-reminder@example.com"},
        )
        assert client.post("/api/auth/logout", headers=first_headers).status_code == 204

        register(client, email="second@example.com")
        second_settings = client.get("/api/email-reminders/settings").json()
        assert second_settings["email_address"] is None

        second_headers = csrf_headers(client)
        client.put(
            "/api/email-reminders/address",
            headers=second_headers,
            json={"email_address": "second-reminder@example.com"},
        )
        assert client.post("/api/auth/logout", headers=second_headers).status_code == 204
        login = client.post(
            "/api/auth/login",
            json={"email": "first@example.com", "password": PASSWORD},
        )
        assert login.status_code == 200
        first_settings = client.get("/api/email-reminders/settings").json()
        assert first_settings["email_address"] == "first-reminder@example.com"
    finally:
        client.__exit__(None, None, None)


def test_missing_dedicated_test_template_is_explicitly_unavailable(tmp_path):
    client, _sender = make_client(tmp_path)
    try:
        register(client)
        response = client.post(
            "/api/email-reminders/test", headers=csrf_headers(client)
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "测试邮件模板尚未配置。"
    finally:
        client.__exit__(None, None, None)


def test_configured_test_email_uses_only_current_verified_destination(tmp_path):
    sender = RecordingEmailSender(test_email_available=True)
    client, sender = make_client(tmp_path, sender=sender)
    try:
        register(client)
        headers = csrf_headers(client)
        client.put(
            "/api/email-reminders/address",
            headers=headers,
            json={"email_address": "test@example.com"},
        )
        client.post("/api/email-reminders/verification/send", headers=headers)
        code = sender.verification_calls[0][1]
        client.post(
            "/api/email-reminders/verification/confirm",
            headers=headers,
            json={"code": code},
        )
        response = client.post("/api/email-reminders/test", headers=headers)
        assert response.status_code == 200
        assert response.json()["message"] == (
            "测试邮件已提交发送，请检查收件箱或垃圾邮件。"
        )
        assert sender.test_calls == ["test@example.com"]
        assert "已收到" not in response.text
    finally:
        client.__exit__(None, None, None)


def test_disabled_provider_exposes_capability_and_blocks_send(tmp_path):
    sender = RecordingEmailSender(available=False)
    client, _sender = make_client(
        tmp_path, sender=sender, provider_enabled=False
    )
    try:
        register(client)
        headers = csrf_headers(client)
        client.put(
            "/api/email-reminders/address",
            headers=headers,
            json={"email_address": "test@example.com"},
        )
        settings = client.get("/api/email-reminders/settings").json()
        assert settings["provider_available"] is False
        response = client.post(
            "/api/email-reminders/verification/send", headers=headers
        )
        assert response.status_code == 503
    finally:
        client.__exit__(None, None, None)


def test_disabled_user_cannot_access_email_api(tmp_path):
    client, _sender = make_client(tmp_path)
    try:
        register(client)
        with client.app.state.database.transaction() as connection:
            connection.execute("UPDATE users SET status='disabled'")
        assert client.get("/api/email-reminders/settings").status_code == 401
        assert client.put(
            "/api/email-reminders/enabled",
            headers=csrf_headers(client),
            json={"enabled": True},
        ).status_code == 401
    finally:
        client.__exit__(None, None, None)
