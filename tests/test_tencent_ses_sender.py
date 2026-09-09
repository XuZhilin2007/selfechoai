from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from requests import Timeout
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.ses.v20201002 import ses_client

from app.config import Settings
from app.services.tencent_ses import (
    EmailProviderUnavailableError,
    EmailSendOutcome,
    EmailStatusQueryOutcome,
    TencentSesEmailSender,
)


REQUESTED_AT = datetime(2026, 9, 3, 16, 30, tzinfo=timezone.utc)


class FakeSesClient:
    def __init__(self):
        self.send_requests = []
        self.status_requests = []
        self.send_response = SimpleNamespace(MessageId="message-id", RequestId="request-id")
        self.status_response = SimpleNamespace(EmailStatusList=[], RequestId="status-request")
        self.send_error = None
        self.status_error = None

    def SendEmail(self, request):
        self.send_requests.append(request)
        if self.send_error is not None:
            raise self.send_error
        return self.send_response

    def GetSendEmailStatus(self, request):
        self.status_requests.append(request)
        if self.status_error is not None:
            raise self.status_error
        return self.status_response


def sender_settings(**updates):
    values = {
        "database_path": "unused.db",
        "email_reminder_provider_enabled": True,
        "tencent_ses_region": "ap-shanghai",
        "tencentcloud_secret_id": SecretStr("secret-id"),
        "tencentcloud_secret_key": SecretStr("secret-key"),
        "tencent_ses_from_email_address": (
            "SelfEcho Community <reminder@community.example>"
        ),
        "tencent_ses_verification_template_id": 101,
        "tencent_ses_reminder_template_id": 102,
        "email_verification_code_pepper": SecretStr("pepper"),
    }
    values.update(updates)
    return Settings(**values)


def test_verification_request_uses_configured_sender_template_and_trigger_type():
    client = FakeSesClient()
    sender = TencentSesEmailSender(sender_settings(), client=client)

    result = sender.send_verification(
        "person@example.com",
        "012345",
        requested_at=REQUESTED_AT,
    )

    request = client.send_requests[0]
    assert request.FromEmailAddress == (
        "SelfEcho Community <reminder@community.example>"
    )
    assert request.Destination == ["person@example.com"]
    assert request.Subject == "SelfEcho 邮箱验证码"
    assert request.TriggerType == 1
    assert request.Template.TemplateID == 101
    assert json.loads(request.Template.TemplateData) == {"code": "012345"}
    assert result.outcome == EmailSendOutcome.ACCEPTED
    assert result.provider_message_id == "message-id"
    assert result.provider_request_id == "request-id"
    assert result.provider_request_date == "2026-09-04"


def test_client_construction_uses_configured_region_fixed_endpoint_and_timeout(
    monkeypatch,
):
    captured = {}
    fake_client = FakeSesClient()

    def create_client(credentials, region, profile):
        captured.update(
            credentials=credentials,
            region=region,
            endpoint=profile.httpProfile.endpoint,
            method=profile.httpProfile.reqMethod,
            timeout=profile.httpProfile.reqTimeout,
        )
        return fake_client

    monkeypatch.setattr(ses_client, "SesClient", create_client)
    sender = TencentSesEmailSender(sender_settings(tencent_ses_timeout_seconds=9.2))

    assert sender.available is True
    assert captured["region"] == "ap-shanghai"
    assert captured["endpoint"] == "ses.tencentcloudapi.com"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 10


def test_reminder_request_contains_only_generic_app_url():
    client = FakeSesClient()
    sender = TencentSesEmailSender(sender_settings(), client=client)

    sender.send_reminder(
        "person@example.com",
        app_url="https://community.example/dashboard",
        requested_at=REQUESTED_AT,
    )

    request = client.send_requests[0]
    assert request.Template.TemplateID == 102
    assert json.loads(request.Template.TemplateData) == {
        "app_url": "https://community.example/dashboard",
    }
    assert request.Subject == "SelfEcho Reminder"
    assert request.Simple is None


@pytest.mark.parametrize("app_url", ["", "https://community.example/\nprivate"])
def test_invalid_reminder_app_url_is_rejected_before_provider_call(app_url):
    client = FakeSesClient()
    sender = TencentSesEmailSender(sender_settings(), client=client)

    with pytest.raises(ValueError, match="app_url"):
        sender.send_reminder("person@example.com", app_url=app_url)
    assert client.send_requests == []


@pytest.mark.parametrize(
    "code",
    [
        "FailedOperation.FrequencyLimit",
        "FailedOperation.ServiceNotAvailable",
        "InternalError",
        "RequestLimitExceeded",
    ],
)
def test_explicit_transient_tencent_errors_are_retryable(code):
    client = FakeSesClient()
    client.send_error = TencentCloudSDKException(code, "private provider text", "req")
    sender = TencentSesEmailSender(sender_settings(), client=client)

    result = sender.send_verification("person@example.com", "123456")

    assert result.outcome == EmailSendOutcome.RETRYABLE_FAILURE
    assert result.error_code == code


def test_unknown_api_error_is_permanent_without_logging_provider_details(caplog):
    client = FakeSesClient()
    client.send_error = TencentCloudSDKException(
        "SomeFuture.Error", "private provider text", "req"
    )
    result = TencentSesEmailSender(sender_settings(), client=client).send_verification(
        "person@example.com", "123456"
    )
    assert result.outcome == EmailSendOutcome.PERMANENT_FAILURE
    assert "private provider text" not in caplog.text
    assert "secret-id" not in caplog.text
    assert "secret-key" not in caplog.text


def test_transport_timeout_is_ambiguous_and_not_retryable():
    client = FakeSesClient()
    client.send_error = Timeout("destination and request content must not escape")
    result = TencentSesEmailSender(sender_settings(), client=client).send_verification(
        "person@example.com", "123456"
    )
    assert result.outcome == EmailSendOutcome.AMBIGUOUS_FAILURE
    assert result.error_code == "transport_ambiguous"


def test_sdk_wrapped_network_error_is_ambiguous_and_not_retryable():
    client = FakeSesClient()
    client.send_error = TencentCloudSDKException(
        "ClientNetworkError", "request may have been transmitted", None
    )
    result = TencentSesEmailSender(sender_settings(), client=client).send_verification(
        "person@example.com", "123456"
    )
    assert result.outcome == EmailSendOutcome.AMBIGUOUS_FAILURE


def test_incomplete_success_response_is_ambiguous():
    client = FakeSesClient()
    client.send_response = SimpleNamespace(MessageId="message-id", RequestId=None)
    result = TencentSesEmailSender(sender_settings(), client=client).send_verification(
        "person@example.com", "123456"
    )
    assert result.outcome == EmailSendOutcome.AMBIGUOUS_FAILURE
    assert result.provider_message_id == "message-id"
    assert result.provider_request_id is None


def test_blacklist_api_error_requests_destination_pause():
    client = FakeSesClient()
    client.send_error = TencentCloudSDKException(
        "FailedOperation.EmailAddrInBlacklist", "blacklisted", "req"
    )
    result = TencentSesEmailSender(sender_settings(), client=client).send_verification(
        "person@example.com", "123456"
    )
    assert result.outcome == EmailSendOutcome.PERMANENT_FAILURE
    assert result.pause_destination is True


def test_status_query_uses_stored_request_date_and_message_id():
    client = FakeSesClient()
    client.status_response = SimpleNamespace(
        EmailStatusList=[
            SimpleNamespace(
                MessageId="message-id",
                ToEmailAddress="person@example.com",
                SendStatus=0,
                DeliverStatus=1,
                DeliverMessage="accepted by recipient server",
                DeliverTime=1_788_484_802,
                _UserComplained=False,
                _UserComplainted=False,
            )
        ],
        RequestId="status-request",
    )
    sender = TencentSesEmailSender(sender_settings(), client=client)

    result = sender.get_send_status(
        provider_message_id="message-id",
        provider_request_date="2026-09-04",
        destination="person@example.com",
    )

    request = client.status_requests[0]
    assert request.RequestDate == "2026-09-04"
    assert request.MessageId == "message-id"
    assert request.ToEmailAddress is None
    assert result.outcome == EmailStatusQueryOutcome.FOUND
    assert result.deliver_status == 1
    assert result.complained is False


def test_status_query_does_not_match_another_destination():
    client = FakeSesClient()
    client.status_response = SimpleNamespace(
        EmailStatusList=[
            SimpleNamespace(
                MessageId="message-id",
                ToEmailAddress="other@example.com",
            )
        ]
    )
    result = TencentSesEmailSender(sender_settings(), client=client).get_send_status(
        provider_message_id="message-id",
        provider_request_date="2026-09-04",
        destination="person@example.com",
    )
    assert result.outcome == EmailStatusQueryOutcome.NOT_FOUND


def test_missing_test_template_is_a_truthful_unavailable_capability():
    sender = TencentSesEmailSender(sender_settings(), client=FakeSesClient())
    assert sender.test_email_available is False
    with pytest.raises(EmailProviderUnavailableError):
        sender.send_test("person@example.com")


def test_configured_test_template_uses_fixed_destination_and_empty_data():
    client = FakeSesClient()
    sender = TencentSesEmailSender(
        sender_settings(tencent_ses_test_template_id=103),
        client=client,
    )

    result = sender.send_test("person@example.com", requested_at=REQUESTED_AT)

    request = client.send_requests[0]
    assert request.Destination == ["person@example.com"]
    assert request.Subject == "SelfEcho 邮件提醒测试"
    assert request.Template.TemplateID == 103
    assert json.loads(request.Template.TemplateData) == {}
    assert result.outcome == EmailSendOutcome.ACCEPTED
