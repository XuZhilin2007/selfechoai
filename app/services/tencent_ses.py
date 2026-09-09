from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout
from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ses.v20201002 import models, ses_client

from app.config import Settings


SES_ENDPOINT = "ses.tencentcloudapi.com"
TENCENT_REQUEST_TIMEZONE = ZoneInfo("Asia/Shanghai")
REMINDER_SUBJECT = "SelfEcho Reminder"
VERIFICATION_SUBJECT = "SelfEcho 邮箱验证码"
TEST_SUBJECT = "SelfEcho 邮件提醒测试"


class EmailProviderUnavailableError(RuntimeError):
    pass


class EmailSendOutcome(str, Enum):
    ACCEPTED = "accepted"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    AMBIGUOUS_FAILURE = "ambiguous_failure"


class EmailStatusQueryOutcome(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    outcome: EmailSendOutcome
    provider_message_id: str | None = None
    provider_request_id: str | None = None
    provider_request_date: str | None = None
    error_code: str | None = None
    pause_destination: bool = False


@dataclass(frozen=True, slots=True)
class EmailStatusResult:
    outcome: EmailStatusQueryOutcome
    send_status: int | None = None
    deliver_status: int | None = None
    deliver_message: str | None = None
    deliver_time: datetime | None = None
    complained: bool = False
    pause_destination: bool = False
    error_code: str | None = None


class TencentSesEmailSender:
    """Small SES boundary; business code never receives Tencent SDK objects."""

    _RETRYABLE_CODES = {
        "FailedOperation.FrequencyLimit",
        "FailedOperation.HighRejectionRate",
        "FailedOperation.ServiceNotAvailable",
        "FailedOperation.TemporaryBlocked",
        "InternalError",
        "InternalError.QueryDataBaseFailed",
        "RequestLimitExceeded",
        "ResourceUnavailable",
    }
    _PAUSE_DESTINATION_CODES = {
        "FailedOperation.EmailAddrInBlacklist",
        "FailedOperation.IncorrectEmail",
        "FailedOperation.ReceiverHasUnsubscribed",
        "FailedOperation.RejectedByRecipients",
        "InvalidParameterValue.IllegalEmailAddress",
        "InvalidParameterValue.ReceiverEmailInvalid",
    }
    _BLACKLIST_SEND_STATUSES = {1007, 1013, 3020}
    _AMBIGUOUS_SDK_CODES = {"ClientNetworkError", "ServerNetworkError"}

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        if settings.email_reminder_provider_enabled and client is None:
            self._client = self._create_client()

    @property
    def available(self) -> bool:
        return self.settings.email_reminder_provider_enabled

    @property
    def test_email_available(self) -> bool:
        return self.available and self.settings.tencent_ses_test_template_id is not None

    def send_verification(
        self,
        destination: str,
        code: str,
        *,
        requested_at: datetime | None = None,
    ) -> EmailSendResult:
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("verification code must contain exactly 6 digits")
        return self._send_template(
            destination=destination,
            subject=VERIFICATION_SUBJECT,
            template_id=self._required_template_id(
                self.settings.tencent_ses_verification_template_id,
                "verification",
            ),
            template_data={"code": code},
            requested_at=requested_at,
        )

    def send_reminder(
        self,
        destination: str,
        *,
        app_url: str,
        requested_at: datetime | None = None,
    ) -> EmailSendResult:
        if not app_url or any(ord(character) < 32 for character in app_url):
            raise ValueError("app_url must be a non-empty URL without controls")
        return self._send_template(
            destination=destination,
            subject=REMINDER_SUBJECT,
            template_id=self._required_template_id(
                self.settings.tencent_ses_reminder_template_id,
                "reminder",
            ),
            template_data={"app_url": app_url},
            requested_at=requested_at,
        )

    def send_test(
        self,
        destination: str,
        *,
        requested_at: datetime | None = None,
    ) -> EmailSendResult:
        template_id = self.settings.tencent_ses_test_template_id
        if template_id is None:
            raise EmailProviderUnavailableError("test email template is unavailable")
        return self._send_template(
            destination=destination,
            subject=TEST_SUBJECT,
            template_id=template_id,
            template_data={},
            requested_at=requested_at,
        )

    def get_send_status(
        self,
        *,
        provider_message_id: str,
        provider_request_date: str,
        destination: str,
    ) -> EmailStatusResult:
        client = self._require_client()
        request = models.GetSendEmailStatusRequest()
        request.RequestDate = provider_request_date
        request.Offset = 0
        request.Limit = 10
        request.MessageId = provider_message_id
        try:
            response = client.GetSendEmailStatus(request)
        except TencentCloudSDKException as exc:
            return EmailStatusResult(
                outcome=EmailStatusQueryOutcome.FAILED,
                error_code=_bounded_code(exc.get_code()),
            )
        except (RequestsTimeout, RequestsConnectionError, TimeoutError, OSError):
            return EmailStatusResult(
                outcome=EmailStatusQueryOutcome.FAILED,
                error_code="transport_error",
            )
        except Exception:
            return EmailStatusResult(
                outcome=EmailStatusQueryOutcome.FAILED,
                error_code="unexpected_provider_error",
            )

        for provider_status in response.EmailStatusList or []:
            if (
                provider_status.MessageId == provider_message_id
                and provider_status.ToEmailAddress == destination
            ):
                complained = bool(
                    getattr(provider_status, "_UserComplained", None)
                    or getattr(provider_status, "_UserComplainted", None)
                )
                send_status = provider_status.SendStatus
                return EmailStatusResult(
                    outcome=EmailStatusQueryOutcome.FOUND,
                    send_status=send_status,
                    deliver_status=provider_status.DeliverStatus,
                    deliver_message=_bounded_message(
                        provider_status.DeliverMessage
                    ),
                    deliver_time=_timestamp_to_datetime(
                        provider_status.DeliverTime
                    ),
                    complained=complained,
                    pause_destination=(
                        complained
                        or send_status in self._BLACKLIST_SEND_STATUSES
                    ),
                )
        return EmailStatusResult(outcome=EmailStatusQueryOutcome.NOT_FOUND)

    def _send_template(
        self,
        *,
        destination: str,
        subject: str,
        template_id: int,
        template_data: dict[str, str],
        requested_at: datetime | None,
    ) -> EmailSendResult:
        client = self._require_client()
        request_time = requested_at or datetime.now(timezone.utc)
        request_date = _tencent_request_date(request_time)
        request = models.SendEmailRequest()
        request.FromEmailAddress = self.settings.tencent_ses_from_email_address
        request.Subject = subject
        request.Destination = [destination]
        request.TriggerType = 1
        template = models.Template()
        template.TemplateID = template_id
        template.TemplateData = json.dumps(
            template_data,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request.Template = template
        try:
            response = client.SendEmail(request)
        except TencentCloudSDKException as exc:
            code = _bounded_code(exc.get_code())
            if code in self._AMBIGUOUS_SDK_CODES:
                outcome = EmailSendOutcome.AMBIGUOUS_FAILURE
            elif code in self._RETRYABLE_CODES:
                outcome = EmailSendOutcome.RETRYABLE_FAILURE
            else:
                outcome = EmailSendOutcome.PERMANENT_FAILURE
            return EmailSendResult(
                outcome=outcome,
                provider_request_id=_bounded_code(exc.get_request_id()),
                provider_request_date=request_date,
                error_code=code or "tencent_api_error",
                pause_destination=code in self._PAUSE_DESTINATION_CODES,
            )
        except (RequestsTimeout, RequestsConnectionError, TimeoutError, OSError):
            return EmailSendResult(
                outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                provider_request_date=request_date,
                error_code="transport_ambiguous",
            )
        except Exception:
            return EmailSendResult(
                outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                provider_request_date=request_date,
                error_code="unexpected_provider_ambiguous",
            )
        message_id = _bounded_code(getattr(response, "MessageId", None))
        request_id = _bounded_code(getattr(response, "RequestId", None))
        if not message_id or not request_id:
            return EmailSendResult(
                outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                provider_message_id=message_id,
                provider_request_id=request_id,
                provider_request_date=request_date,
                error_code="incomplete_acceptance_response",
            )
        return EmailSendResult(
            outcome=EmailSendOutcome.ACCEPTED,
            provider_message_id=message_id,
            provider_request_id=request_id,
            provider_request_date=request_date,
        )

    def _create_client(self) -> ses_client.SesClient:
        secret_id = self.settings.tencentcloud_secret_id.get_secret_value()
        secret_key = self.settings.tencentcloud_secret_key.get_secret_value()
        if not secret_id or not secret_key:
            raise EmailProviderUnavailableError(
                "Tencent SES credentials are unavailable"
            )
        http_profile = HttpProfile(
            endpoint=SES_ENDPOINT,
            reqMethod="POST",
            reqTimeout=max(1, math.ceil(self.settings.tencent_ses_timeout_seconds)),
        )
        client_profile = ClientProfile(httpProfile=http_profile)
        return ses_client.SesClient(
            credential.Credential(secret_id, secret_key),
            self.settings.tencent_ses_region,
            client_profile,
        )

    def _require_client(self) -> Any:
        if not self.available or self._client is None:
            raise EmailProviderUnavailableError("Email Reminder is unavailable")
        return self._client

    @staticmethod
    def _required_template_id(value: int | None, purpose: str) -> int:
        if value is None:
            raise EmailProviderUnavailableError(
                f"Tencent SES {purpose} template is unavailable"
            )
        return value


def _tencent_request_date(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("requested_at must be timezone-aware")
    return value.astimezone(TENCENT_REQUEST_TIMEZONE).date().isoformat()


def _timestamp_to_datetime(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0)


def _bounded_code(value: str | None) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized[:200] or None


def _bounded_message(value: str | None) -> str | None:
    normalized = " ".join(str(value).split()) if value else ""
    return normalized[:500] or None
