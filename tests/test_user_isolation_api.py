from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.schemas import AIExtraction, AIItemFields, ItemStatus
from app.services.ai import AINetworkError, AIService


INVITE_CODE = "isolation-api-invite"
PASSWORD = "isolation API test password"


class IsolationAIService(AIService):
    async def extract(self, original_text, existing_item):
        if "FORCE_FAILURE" in original_text:
            raise AINetworkError("forced isolation test failure")
        if existing_item is not None:
            return AIExtraction(
                fields=AIItemFields(next_action="重新整理完成"),
                evidence_fields=set(),
            )
        return AIExtraction(
            fields=AIItemFields(
                title=original_text,
                type="note",
                status=ItemStatus.ACTIVE,
            ),
            evidence_fields=set(),
        )


def register_user(client: TestClient, email: str, display_name: str) -> int:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE_CODE,
            "email": email,
            "password": PASSWORD,
            "display_name": display_name,
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201
    csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf_token is not None
    client.headers["X-CSRF-Token"] = csrf_token
    return response.json()["id"]


@pytest.fixture
def two_user_clients(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "api-isolation.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE_CODE),
        app_origin="http://testserver",
        session_cookie_secure=False,
    )
    app = create_app(settings=settings, ai_service=IsolationAIService())
    with TestClient(app) as user_a_client, TestClient(app) as user_b_client:
        user_a_id = register_user(
            user_a_client, "user-a@example.com", "User A"
        )
        user_b_id = register_user(
            user_b_client, "user-b@example.com", "User B"
        )
        yield app, user_a_client, user_b_client, user_a_id, user_b_id


def create_item(client: TestClient, title: str) -> int:
    response = client.post("/api/inputs", json={"original_text": title})
    assert response.status_code == 202
    dashboard = client.get("/api/items").json()
    items = dashboard["sortable_items"] + dashboard["needs_confirmation"]
    return next(item["id"] for item in items if item["title"] == title)


def test_each_user_only_sees_their_own_dashboard_items(two_user_clients):
    _, user_a, user_b, _, _ = two_user_clients
    create_item(user_a, "A 的事项")
    create_item(user_b, "B 的事项")

    dashboard_a = user_a.get("/api/items").json()
    dashboard_b = user_b.get("/api/items").json()
    items_a = dashboard_a["sortable_items"] + dashboard_a["needs_confirmation"]
    items_b = dashboard_b["sortable_items"] + dashboard_b["needs_confirmation"]

    assert {item["title"] for item in items_a} == {"A 的事项"}
    assert {item["title"] for item in items_b} == {"B 的事项"}


def test_cross_user_item_operations_return_404(two_user_clients):
    _, user_a, user_b, _, _ = two_user_clients
    item_b_id = create_item(user_b, "B 的私有事项")

    assert user_a.get(f"/api/items/{item_b_id}").status_code == 404
    assert user_a.patch(
        f"/api/items/{item_b_id}", json={"title": "越权修改"}
    ).status_code == 404
    assert user_a.delete(f"/api/items/{item_b_id}").status_code == 404
    assert user_a.post(
        f"/api/items/{item_b_id}/reprocess"
    ).status_code == 404
    assert user_a.post(
        f"/api/items/{item_b_id}/inputs",
        json={"original_text": "越权补充"},
    ).status_code == 404

    assert user_b.get(f"/api/items/{item_b_id}").status_code == 200
    assert user_b.get(f"/api/items/{item_b_id}").json()["item"][
        "title"
    ] == "B 的私有事项"


def test_user_cannot_retry_another_users_failed_input(two_user_clients):
    _, user_a, user_b, _, _ = two_user_clients
    failed = user_b.post(
        "/api/inputs",
        json={"original_text": "FORCE_FAILURE B 的输入"},
    )
    assert failed.status_code == 202
    input_id = failed.json()["id"]
    dashboard_b = user_b.get("/api/items").json()
    assert dashboard_b["failed_inputs"][0]["id"] == input_id

    response = user_a.post(f"/api/inputs/{input_id}/retry")

    assert response.status_code == 404
    assert user_b.get("/api/items").json()["failed_inputs"][0]["id"] == input_id


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("post", "/api/inputs", {"original_text": "未登录输入"}),
        ("post", "/api/items/1/inputs", {"original_text": "未登录补充"}),
        ("post", "/api/inputs/1/retry", None),
        ("post", "/api/items/1/reprocess", None),
        ("get", "/api/items", None),
        ("get", "/api/items/1", None),
        ("patch", "/api/items/1", {"title": "未登录修改"}),
        ("delete", "/api/items/1", None),
    ],
)
def test_business_routes_require_authentication(
    two_user_clients,
    method,
    path,
    json_body,
):
    app, _, _, _, _ = two_user_clients
    with TestClient(app) as anonymous:
        response = anonymous.request(method, path, json=json_body)

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path_template", "json_body"),
    [
        ("post", "/api/inputs", {"original_text": "缺少 CSRF"}),
        ("post", "/api/items/{item_id}/inputs", {"original_text": "缺少 CSRF"}),
        ("post", "/api/inputs/1/retry", None),
        ("post", "/api/items/{item_id}/reprocess", None),
        ("patch", "/api/items/{item_id}", {"title": "缺少 CSRF"}),
        ("delete", "/api/items/{item_id}", None),
    ],
)
def test_authenticated_writes_require_csrf(
    two_user_clients,
    method,
    path_template,
    json_body,
):
    _, user_a, _, _, _ = two_user_clients
    item_id = create_item(user_a, "用于 CSRF 测试的事项")
    csrf_token = user_a.headers.pop("X-CSRF-Token")
    try:
        response = user_a.request(
            method,
            path_template.format(item_id=item_id),
            json=json_body,
        )
        get_response = user_a.get("/api/items")
    finally:
        user_a.headers["X-CSRF-Token"] = csrf_token

    assert response.status_code == 403
    assert get_response.status_code == 200
