from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.schemas import AIExtraction, PersonalItemPublic
from app.services.ai import AIService


class FunctionAIService(AIService):
    def __init__(
        self,
        handler: Callable[[str, PersonalItemPublic | None], AIExtraction],
    ) -> None:
        self.handler = handler
        self.calls = 0

    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        self.calls += 1
        return self.handler(original_text, existing_item)


TEST_INVITE_CODE = "workflow-test-invite"
TEST_PASSWORD = "workflow test password"


@pytest.fixture
def client_factory(tmp_path: Path):
    clients: list[TestClient] = []

    def factory(ai_service: AIService) -> TestClient:
        database_path = tmp_path / f"test-{len(clients)}.db"
        settings = Settings(
            database_path=database_path,
            registration_mode="invite",
            invite_code_hash=hash_invite_code(TEST_INVITE_CODE),
            app_origin="http://testserver",
            session_cookie_secure=False,
        )
        client = TestClient(create_app(settings=settings, ai_service=ai_service))
        client.__enter__()
        registration = client.post(
            "/api/auth/register",
            json={
                "invite_code": TEST_INVITE_CODE,
                "email": f"workflow-{len(clients)}@example.com",
                "password": TEST_PASSWORD,
                "display_name": "Workflow User",
                "timezone": "Asia/Shanghai",
            },
        )
        assert registration.status_code == 201
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        assert csrf_token is not None
        client.headers["X-CSRF-Token"] = csrf_token
        clients.append(client)
        return client

    yield factory

    for client in reversed(clients):
        client.__exit__(None, None, None)
