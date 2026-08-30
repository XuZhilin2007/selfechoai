from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.repository import NotFoundError
from app.services.push_security import PushEndpointPolicy


INVITE_CODE = "push-api-synthetic-invite"
PASSWORD = "push API synthetic password"


def push_app(
    database_path: Path,
    vapid_key_pair: tuple[str, str],
    *,
    test_send: bool = False,
):
    public_key, private_key = vapid_key_pair
    settings = Settings(
        database_path=database_path,
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE_CODE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        web_push_enabled=True,
        web_push_vapid_public_key=public_key,
        web_push_vapid_private_key=SecretStr(private_key),
        web_push_vapid_subject="mailto:push@example.com",
        web_push_test_send_enabled=test_send,
    )
    return create_app(
        settings=settings,
        push_endpoint_policy=PushEndpointPolicy(
            lambda _hostname, _port: ("8.8.8.8",)
        ),
    )


def register(client: TestClient, email: str = "push-owner@example.com") -> int:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE_CODE,
            "email": email,
            "password": PASSWORD,
            "display_name": "Push Owner",
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    client.headers["X-CSRF-Token"] = csrf
    return response.json()["id"]


def login(client: TestClient, email: str = "push-owner@example.com") -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    client.headers["X-CSRF-Token"] = csrf


def payload(endpoint: str, subscription_keys: dict[str, str]) -> dict[str, object]:
    return {"endpoint": endpoint, "keys": subscription_keys}


def test_push_config_is_authenticated_and_never_returns_private_key(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
):
    app = push_app(tmp_path / "config-api.db", vapid_key_pair)
    with TestClient(app) as client:
        assert client.get("/api/push/config").status_code == 401
        register(client)

        response = client.get("/api/push/config")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "vapid_public_key": vapid_key_pair[0],
    }
    assert vapid_key_pair[1] not in response.text


def test_subscription_write_requires_csrf_and_returns_only_public_fields(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "csrf.db", vapid_key_pair)
    with TestClient(app) as client:
        register(client)
        client.headers.pop("X-CSRF-Token")
        request_payload = payload(
            "https://push.example.test/send/device-a",
            subscription_keys,
        )
        assert client.put("/api/push/subscriptions", json=request_payload).status_code == 403
        client.headers["X-CSRF-Token"] = client.cookies.get(CSRF_COOKIE_NAME)

        response = client.put("/api/push/subscriptions", json=request_payload)

    assert response.status_code == 200
    assert response.json() == {"id": 1, "status": "active"}
    assert "endpoint" not in response.text
    assert subscription_keys["p256dh"] not in response.text
    assert subscription_keys["auth"] not in response.text


def test_disable_and_test_send_writes_require_csrf(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "write-csrf.db", vapid_key_pair, test_send=True)
    with TestClient(app) as client:
        register(client)
        client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/csrf", subscription_keys),
        )
        client.headers.pop("X-CSRF-Token")

        disable = client.delete("/api/push/subscriptions/current")
        test_send = client.post("/api/push/test")

    assert disable.status_code == 403
    assert test_send.status_code == 403


def test_same_session_update_and_replacement_have_one_active_binding(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "replacement.db", vapid_key_pair)
    first_endpoint = "https://push.example.test/send/first"
    second_endpoint = "https://push.example.test/send/second"
    updated_keys = dict(subscription_keys)
    updated_keys["auth"] = base64.urlsafe_b64encode(b"updated-auth-key").rstrip(b"=").decode()
    with TestClient(app) as client:
        user_id = register(client)
        first = client.put(
            "/api/push/subscriptions",
            json=payload(first_endpoint, subscription_keys),
        )
        updated = client.put(
            "/api/push/subscriptions",
            json=payload(first_endpoint, updated_keys),
        )
        replacement = client.put(
            "/api/push/subscriptions",
            json=payload(second_endpoint, subscription_keys),
        )

        active = app.state.reminder_repository.list_active_push_subscriptions(user_id)
        with app.state.database.connection() as connection:
            old_status = connection.execute(
                "SELECT status FROM push_subscriptions WHERE endpoint = ?",
                (first_endpoint,),
            ).fetchone()[0]

    assert first.json()["id"] == updated.json()["id"]
    assert replacement.json()["id"] != first.json()["id"]
    assert [record.endpoint.get_secret_value() for record in active] == [second_endpoint]
    assert old_status == "revoked"


def test_current_device_disable_and_logout_preserve_other_device(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "devices.db", vapid_key_pair)
    with TestClient(app) as device_a, TestClient(app) as device_b:
        user_id = register(device_a)
        device_a.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/a", subscription_keys),
        )
        login(device_b)
        device_b.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/b", subscription_keys),
        )

        disabled = device_b.delete("/api/push/subscriptions/current")
        active_after_disable = app.state.reminder_repository.list_active_push_subscriptions(
            user_id
        )
        device_b.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/b", subscription_keys),
        )
        logout = device_b.post("/api/auth/logout")
        active_after_logout = app.state.reminder_repository.list_active_push_subscriptions(
            user_id
        )

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "revoked"
    assert logout.status_code == 204
    assert [item.endpoint.get_secret_value() for item in active_after_disable] == [
        "https://push.example.test/send/a"
    ]
    assert [item.endpoint.get_secret_value() for item in active_after_logout] == [
        "https://push.example.test/send/a"
    ]


def test_cross_user_endpoint_collision_and_revoke_are_isolated(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "isolation.db", vapid_key_pair)
    endpoint = "https://push.example.test/send/private-device"
    with TestClient(app) as user_a, TestClient(app) as user_b:
        user_a_id = register(user_a, "push-a@example.com")
        user_b_id = register(user_b, "push-b@example.com")
        created = user_a.put(
            "/api/push/subscriptions",
            json=payload(endpoint, subscription_keys),
        )

        collision = user_b.put(
            "/api/push/subscriptions",
            json=payload(endpoint, subscription_keys),
        )
        cross_revoke = user_b.delete(
            f"/api/push/subscriptions/{created.json()['id']}"
        )
        active_a = app.state.reminder_repository.list_active_push_subscriptions(user_a_id)
        active_b = app.state.reminder_repository.list_active_push_subscriptions(user_b_id)
        with pytest.raises(NotFoundError):
            app.state.reminder_repository.get_push_subscription(
                created.json()["id"],
                user_b_id,
            )

    assert collision.status_code == 422
    assert collision.json() == {"detail": "invalid push subscription"}
    assert cross_revoke.status_code == 404
    assert len(active_a) == 1
    assert active_b == []


def test_expired_session_subscription_is_not_active(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "expired.db", vapid_key_pair)
    with TestClient(app) as client:
        user_id = register(client)
        client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/expired", subscription_keys),
        )
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE user_sessions SET expires_time = ? WHERE user_id = ?",
                (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(), user_id),
            )

        active = app.state.reminder_repository.list_active_push_subscriptions(user_id)

    assert active == []


def test_user_without_current_subscription_cannot_test_send_another_users_device(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "test-isolation.db", vapid_key_pair, test_send=True)
    with TestClient(app) as user_a, TestClient(app) as user_b:
        register(user_a, "test-a@example.com")
        register(user_b, "test-b@example.com")
        user_a.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/a-only", subscription_keys),
        )
        app.state.web_push_service.sender = lambda **_kwargs: SimpleNamespace(
            status_code=201
        )

        response = user_b.post("/api/push/test")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "no active subscription for current session"
    }


def test_logout_revokes_subscription_before_account_switch(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "switch.db", vapid_key_pair)
    with TestClient(app) as client:
        first_user = register(client, "first@example.com")
        client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/first-user", subscription_keys),
        )
        assert client.post("/api/auth/logout").status_code == 204
        second_user = register(client, "second@example.com")
        client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/second-user", subscription_keys),
        )

        first_active = app.state.reminder_repository.list_active_push_subscriptions(
            first_user
        )
        second_active = app.state.reminder_repository.list_active_push_subscriptions(
            second_user
        )

    assert first_active == []
    assert len(second_active) == 1


def test_test_send_uses_only_current_session_and_invalidates_gone_subscription(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "test-send.db", vapid_key_pair, test_send=True)
    with TestClient(app) as client:
        user_id = register(client)
        created = client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send/test", subscription_keys),
        )
        app.state.web_push_service.sender = lambda **_kwargs: SimpleNamespace(
            status_code=410
        )

        response = client.post("/api/push/test")
        active = app.state.reminder_repository.list_active_push_subscriptions(user_id)
        with app.state.database.connection() as connection:
            stored = connection.execute(
                "SELECT status, last_error_code FROM push_subscriptions WHERE id = ?",
                (created.json()["id"],),
            ).fetchone()

    assert response.status_code == 200
    assert response.json() == {
        "outcome": "subscription_gone",
        "provider_status": 410,
    }
    assert active == []
    assert tuple(stored) == ("invalid", "http_410")


def test_subscription_request_size_limits_are_enforced(
    tmp_path: Path,
    vapid_key_pair: tuple[str, str],
    subscription_keys: dict[str, str],
):
    app = push_app(tmp_path / "limits.db", vapid_key_pair)
    with TestClient(app) as client:
        register(client)

        endpoint_response = client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/" + "a" * 2_100, subscription_keys),
        )
        oversized_keys = dict(subscription_keys)
        oversized_keys["p256dh"] = "a" * 257
        key_response = client.put(
            "/api/push/subscriptions",
            json=payload("https://push.example.test/send", oversized_keys),
        )

    assert endpoint_response.status_code == 422
    assert key_response.status_code == 422
