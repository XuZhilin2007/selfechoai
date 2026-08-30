from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests
from pywebpush import WebPushException, webpush
from requests import RequestException, Timeout

from app.config import Settings
from app.schemas import (
    PushSubscriptionRecord,
    WebPushOutcome,
    WebPushPayload,
    WebPushPayloadType,
)
from app.services.push_security import PushEndpointPolicy, UnsafePushEndpointError


GENERIC_REMINDER_BODY = "你有一条 SelfEcho 微提醒。"
TEST_NOTIFICATION_BODY = "这是一条 SelfEcho 测试通知。"


class WebPushSender(Protocol):
    def __call__(self, **kwargs: Any) -> object: ...


class NoRedirectSession(requests.Session):
    """A bounded pywebpush session that never follows provider redirects."""

    def __init__(self) -> None:
        super().__init__()
        self.trust_env = False

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any):
        kwargs["allow_redirects"] = False
        kwargs["stream"] = True
        response = super().post(url, data=data, json=json, **kwargs)
        response.close()
        response._content = b""
        response._content_consumed = True
        return response


@dataclass(frozen=True, slots=True)
class WebPushResult:
    outcome: WebPushOutcome
    provider_status: int | None = None
    error_code: str | None = None


def build_reminder_push_payload() -> WebPushPayload:
    """Build a generic payload without Reminder, Item, or user identifiers."""

    return WebPushPayload(
        type=WebPushPayloadType.REMINDER,
        title="SelfEcho",
        body=GENERIC_REMINDER_BODY,
        target_path="/dashboard",
    )


def build_test_push_payload() -> WebPushPayload:
    return WebPushPayload(
        type=WebPushPayloadType.TEST,
        title="SelfEcho",
        body=TEST_NOTIFICATION_BODY,
        target_path="/dashboard",
    )


class WebPushService:
    """One-attempt Web Push transport with bounded and redacted outcomes."""

    def __init__(
        self,
        settings: Settings,
        endpoint_policy: PushEndpointPolicy,
        *,
        sender: WebPushSender = webpush,
        requests_session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.endpoint_policy = endpoint_policy
        self.sender = sender
        self.requests_session = (
            requests_session
            if requests_session is not None
            else NoRedirectSession()
        )

    def send(
        self,
        subscription: PushSubscriptionRecord,
        payload: WebPushPayload,
    ) -> WebPushResult:
        if not self.settings.web_push_enabled:
            return WebPushResult(
                WebPushOutcome.CONFIGURATION_ERROR,
                error_code="web_push_disabled",
            )

        endpoint = subscription.endpoint.get_secret_value()
        try:
            self.endpoint_policy.validate(endpoint)
        except UnsafePushEndpointError:
            return WebPushResult(
                WebPushOutcome.PROVIDER_ERROR,
                error_code="unsafe_endpoint",
            )

        subscription_info = {
            "endpoint": endpoint,
            "keys": {
                "p256dh": subscription.p256dh.get_secret_value(),
                "auth": subscription.auth.get_secret_value(),
            },
        }
        try:
            response = self.sender(
                subscription_info=subscription_info,
                data=payload.model_dump_json(),
                vapid_private_key=(
                    self.settings.web_push_vapid_private_key.get_secret_value()
                ),
                vapid_claims={"sub": self.settings.web_push_vapid_subject},
                timeout=self.settings.web_push_timeout_seconds,
                ttl=60,
                verbose=False,
                requests_session=self.requests_session,
            )
        except Timeout:
            return WebPushResult(WebPushOutcome.TRANSIENT_ERROR, error_code="timeout")
        except RequestException:
            return WebPushResult(
                WebPushOutcome.TRANSIENT_ERROR,
                error_code="network_error",
            )
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if isinstance(status_code, int):
                return self._classify_status(status_code)
            return WebPushResult(
                WebPushOutcome.CONFIGURATION_ERROR,
                error_code="web_push_configuration_error",
            )
        except (IndexError, OSError, TimeoutError, TypeError, ValueError):
            return WebPushResult(
                WebPushOutcome.CONFIGURATION_ERROR,
                error_code="invalid_transport_input",
            )

        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            return WebPushResult(
                WebPushOutcome.PROVIDER_ERROR,
                error_code="invalid_provider_response",
            )
        return self._classify_status(status_code)

    @staticmethod
    def _classify_status(status_code: int) -> WebPushResult:
        if 200 <= status_code < 300:
            return WebPushResult(WebPushOutcome.ACCEPTED, status_code)
        if status_code in {404, 410}:
            return WebPushResult(
                WebPushOutcome.SUBSCRIPTION_GONE,
                status_code,
                f"http_{status_code}",
            )
        if status_code in {401, 403}:
            return WebPushResult(
                WebPushOutcome.AUTHENTICATION_ERROR,
                status_code,
                f"http_{status_code}",
            )
        if status_code == 429 or status_code >= 500:
            return WebPushResult(
                WebPushOutcome.TRANSIENT_ERROR,
                status_code,
                f"http_{status_code}",
            )
        return WebPushResult(
            WebPushOutcome.PROVIDER_ERROR,
            status_code,
            f"http_{status_code}",
        )
