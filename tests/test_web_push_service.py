from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from pywebpush import WebPushException
from requests import Response, Timeout
from requests.adapters import BaseAdapter

from app.config import Settings
from app.schemas import PushSubscriptionRecord, PushSubscriptionStatus, WebPushOutcome
from app.services.push_security import PushEndpointPolicy
from app.services.web_push import (
    NoRedirectSession,
    WebPushService,
    build_reminder_push_payload,
    build_test_push_payload,
)


def push_settings(
    database_path: Path,
    vapid_key_pair: tuple[str, str],
) -> Settings:
    public_key, private_key = vapid_key_pair
    return Settings(
        database_path=database_path,
        web_push_enabled=True,
        web_push_vapid_public_key=public_key,
        web_push_vapid_private_key=SecretStr(private_key),
        web_push_vapid_subject="https://selfecho.example",
        web_push_timeout_seconds=4.5,
    )


def subscription(
    subscription_keys: dict[str, str],
    endpoint: str = "https://push.example.test/send/synthetic-token",
) -> PushSubscriptionRecord:
    now = datetime.now(timezone.utc)
    return PushSubscriptionRecord(
        id=1,
        user_id=2,
        session_id=3,
        endpoint=SecretStr(endpoint),
        p256dh=SecretStr(subscription_keys["p256dh"]),
        auth=SecretStr(subscription_keys["auth"]),
        status=PushSubscriptionStatus.ACTIVE,
        created_time=now,
        updated_time=now,
        invalidated_time=None,
        last_error_code=None,
    )


class RedirectAdapter(BaseAdapter):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.requests: list[str] = []
        self.send_kwargs: list[dict[str, object]] = []

    def send(self, request, **kwargs):
        self.requests.append(request.url)
        self.send_kwargs.append(kwargs)
        response = Response()
        response.status_code = self.status_code
        response.url = request.url
        response.request = request
        response.headers["Location"] = "http://127.0.0.1/internal"
        response._content = b""
        response._content_consumed = True
        return response

    def close(self) -> None:
        return None


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_transport_session_never_follows_redirects(status_code: int):
    adapter = RedirectAdapter(status_code)
    session = NoRedirectSession()
    session.mount("https://", adapter)

    response = session.post("https://push.example.test/first", timeout=1.0)

    assert response.status_code == status_code
    assert response.content == b""
    assert response.history == []
    assert adapter.requests == ["https://push.example.test/first"]
    assert adapter.send_kwargs[0]["stream"] is True
    assert session.trust_env is False


def test_web_push_passes_finite_timeout_no_redirect_session_and_generic_payload(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    calls: list[dict[str, object]] = []

    def sender(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=201)

    service = WebPushService(
        push_settings(tmp_path / "transport.db", vapid_key_pair),
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)),
        sender=sender,
    )

    result = service.send(subscription(subscription_keys), build_test_push_payload())

    assert result.outcome == WebPushOutcome.ACCEPTED
    assert len(calls) == 1
    assert calls[0]["timeout"] == 4.5
    assert calls[0]["ttl"] == 60
    assert calls[0]["verbose"] is False
    assert isinstance(calls[0]["requests_session"], NoRedirectSession)
    payload = json.loads(str(calls[0]["data"]))
    assert payload == {
        "type": "test",
        "title": "SelfEcho",
        "body": "这是一条 SelfEcho 测试通知。",
        "target_path": "/dashboard",
    }
    assert "user_id" not in payload
    assert "item_id" not in payload
    assert "reminder_id" not in payload


@pytest.mark.parametrize("status_code", [201, 301, 302, 307, 308])
def test_real_pywebpush_uses_bounded_session_timeout_and_one_request(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
    status_code: int,
):
    adapter = RedirectAdapter(status_code)
    session = NoRedirectSession()
    session.mount("https://", adapter)
    service = WebPushService(
        push_settings(tmp_path / f"real-pywebpush-{status_code}.db", vapid_key_pair),
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)),
        requests_session=session,
    )

    result = service.send(subscription(subscription_keys), build_test_push_payload())

    assert adapter.requests == [
        "https://push.example.test/send/synthetic-token"
    ]
    assert adapter.send_kwargs[0]["timeout"] == 4.5
    assert adapter.send_kwargs[0]["stream"] is True
    expected = (
        WebPushOutcome.ACCEPTED
        if status_code == 201
        else WebPushOutcome.PROVIDER_ERROR
    )
    assert result.outcome == expected


def test_reminder_payload_omits_item_and_reminder_identifiers():
    payload = build_reminder_push_payload().model_dump()

    assert payload["target_path"] == "/dashboard"
    assert "item_id" not in payload
    assert "reminder_id" not in payload


def test_web_push_revalidates_dns_immediately_before_sender(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    dns_calls = 0
    sender_calls = 0

    def resolver(_hostname: str, _port: int):
        nonlocal dns_calls
        dns_calls += 1
        return ("10.0.0.9",)

    def sender(**_kwargs):
        nonlocal sender_calls
        sender_calls += 1
        return SimpleNamespace(status_code=201)

    service = WebPushService(
        push_settings(tmp_path / "blocked.db", vapid_key_pair),
        PushEndpointPolicy(resolver),
        sender=sender,
    )

    result = service.send(subscription(subscription_keys), build_test_push_payload())

    assert result.outcome == WebPushOutcome.PROVIDER_ERROR
    assert result.error_code == "unsafe_endpoint"
    assert dns_calls == 1
    assert sender_calls == 0


def test_timeout_is_normalized_without_exposing_subscription_data(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    def sender(**_kwargs):
        raise Timeout("synthetic timeout containing no remote request")

    record = subscription(subscription_keys)
    service = WebPushService(
        push_settings(tmp_path / "timeout.db", vapid_key_pair),
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)),
        sender=sender,
    )

    result = service.send(record, build_test_push_payload())

    assert result.outcome == WebPushOutcome.TRANSIENT_ERROR
    assert result.error_code == "timeout"
    assert record.endpoint.get_secret_value() not in repr(result)
    assert subscription_keys["p256dh"] not in repr(record)
    assert subscription_keys["auth"] not in repr(record)


def test_remote_error_body_is_not_propagated(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    remote_body = "synthetic remote body that must remain discarded"
    response = Response()
    response.status_code = 500
    response._content = remote_body.encode()

    def sender(**_kwargs):
        raise WebPushException("provider rejected request", response=response)

    service = WebPushService(
        push_settings(tmp_path / "remote-error.db", vapid_key_pair),
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)),
        sender=sender,
    )

    result = service.send(subscription(subscription_keys), build_test_push_payload())

    assert result.outcome == WebPushOutcome.TRANSIENT_ERROR
    assert result.provider_status == 500
    assert remote_body not in repr(result)


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_redirect_statuses_are_normalized_as_provider_errors(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
    status_code: int,
):
    service = WebPushService(
        push_settings(tmp_path / f"redirect-{status_code}.db", vapid_key_pair),
        PushEndpointPolicy(lambda _hostname, _port: ("8.8.8.8",)),
        sender=lambda **_kwargs: SimpleNamespace(status_code=status_code),
    )

    result = service.send(subscription(subscription_keys), build_test_push_payload())

    assert result.outcome == WebPushOutcome.PROVIDER_ERROR
    assert result.provider_status == status_code
