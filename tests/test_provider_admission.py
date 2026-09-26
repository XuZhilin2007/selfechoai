from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.provider_admission import GlobalProviderLimiter, ProviderAdmissionDenied
from app.services.ai import (
    AIAPIError,
    AIAdmissionError,
    AIInputSizeError,
    OpenAIResponsesAIService,
)
from app.services.deepseek import DeepSeekProvider


INVITE_CODE = "provider-admission-invite"
PASSWORD = "a strong test password"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def extraction_json() -> str:
    return json.dumps({
        "fields": {"title": "保留记录", "type": "other", "status": "active"},
        "evidence_fields": [],
        "reminder": {"intent": False, "temporal_expression": None},
    }, ensure_ascii=False)


def test_global_admission_counts_calls_and_releases_concurrency() -> None:
    clock = Clock()
    guard = GlobalProviderLimiter(10, 2, 1, clock=clock)
    first = guard.acquire()
    with pytest.raises(ProviderAdmissionDenied):
        guard.acquire()
    first.release()
    first.release()
    with guard.acquire():
        pass
    with pytest.raises(ProviderAdmissionDenied) as denied:
        guard.acquire()
    assert denied.value.retry_after == 10
    clock.now = 10
    with guard.acquire():
        pass
    # Restart-reset state is a new instance, not persistent accounting.
    with GlobalProviderLimiter(10, 1, 1, clock=clock).acquire():
        pass


def test_cancellation_releases_provider_concurrency() -> None:
    async def scenario() -> None:
        guard = GlobalProviderLimiter(60, 2, 1)
        entered = asyncio.Event()

        async def pending() -> None:
            with guard.acquire():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        await entered.wait()
        with pytest.raises(ProviderAdmissionDenied):
            guard.acquire()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with guard.acquire():
            pass

    asyncio.run(scenario())


def test_deepseek_repair_is_a_second_admitted_provider_call() -> None:
    async def scenario(limit: int) -> tuple[int, str | None]:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            content = "not valid JSON" if calls == 1 else extraction_json()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}]
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                "https://example.test", "synthetic-key", client=client,
                admission=GlobalProviderLimiter(60, limit, 1),
            )
            try:
                result = await provider.extract(
                    "用户原文", None, current_local_date=date(2026, 9, 25)
                )
            except AIAdmissionError:
                return calls, None
            return calls, result.fields.title

    assert asyncio.run(scenario(1)) == (1, None)
    assert asyncio.run(scenario(2)) == (2, "保留记录")


@pytest.mark.parametrize("provider_kind", ["deepseek", "openai"])
def test_provider_bound_body_size_rejects_before_any_outbound_call(provider_kind: str) -> None:
    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            guard = GlobalProviderLimiter(60, 1, 1)
            if provider_kind == "deepseek":
                provider = DeepSeekProvider(
                    "https://example.test", "synthetic-key", client=client,
                    admission=guard, max_input_bytes=1_000,
                )
            else:
                provider = OpenAIResponsesAIService(
                    "https://example.test", "synthetic-key", "synthetic-model", 1,
                    client=client, admission=guard, max_input_bytes=1_000,
                )
            with pytest.raises(AIInputSizeError):
                await provider.extract(
                    "Reprocess full Item Input history: " + "历史原文" * 1_000,
                    None, current_local_date=date(2026, 9, 25),
                )
            assert calls == 0
            with guard.acquire():
                pass

    asyncio.run(scenario())


def test_openai_outbound_concurrency_is_released_after_provider_failure() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return httpx.Response(503)
            return httpx.Response(200, json={"output_text": extraction_json()})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesAIService(
                "https://example.test", "synthetic-key", "synthetic-model", 1,
                client=client, admission=GlobalProviderLimiter(60, 2, 1),
            )
            first = asyncio.create_task(provider.extract(
                "first", None, current_local_date=date(2026, 9, 25)
            ))
            await entered.wait()
            with pytest.raises(AIAdmissionError):
                await provider.extract("second", None, current_local_date=date(2026, 9, 25))
            assert calls == 1
            release.set()
            with pytest.raises(AIAPIError):
                await first
            result = await provider.extract(
                "third", None, current_local_date=date(2026, 9, 25)
            )
            assert result.fields.title == "保留记录"
            assert calls == 2

    asyncio.run(scenario())


def test_application_uses_one_guard_for_all_internal_ai_processing(tmp_path) -> None:
    app = create_app(Settings(
        database_path=tmp_path / "app.db",
        registration_mode="closed",
        deepseek_api_key="synthetic-key",
    ))
    assert isinstance(app.state.processor.ai_service, DeepSeekProvider)
    assert app.state.processor.ai_service.admission is app.state.provider_admission.ai


def test_saved_capture_retry_and_reprocess_share_actual_ai_call_limit(tmp_path) -> None:
    clock = Clock()
    settings = Settings(
        database_path=tmp_path / "app.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE_CODE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        deepseek_api_key="synthetic-key",
        ai_admission_call_limit=2,
    )
    app = create_app(settings, provider_admission_clock=clock)
    provider = app.state.processor.ai_service
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": extraction_json()},
                         "finish_reason": "stop"}]
        })

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with TestClient(app) as client:
            registered = client.post("/api/auth/register", json={
                "invite_code": INVITE_CODE,
                "email": "provider-guard@example.test", "password": "synthetic password",
                "display_name": "Test", "timezone": "UTC",
            })
            assert registered.status_code == 201
            client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]

            first = client.post("/api/inputs", json={"original_text": "第一条完整原文"})
            assert first.status_code == 202
            item = client.get("/api/items").json()["sortable_items"][0]
            appended = client.post(f"/api/items/{item['id']}/inputs", json={
                "original_text": "第二条补充原文",
            })
            assert appended.status_code == 202
            assert len(calls) == 2

            denied = client.post("/api/inputs", json={"original_text": "必须保留的第三条原文"})
            assert denied.status_code == 202
            failed = client.get("/api/items").json()["failed_inputs"][0]
            assert failed["original_text"] == "必须保留的第三条原文"
            assert "临时使用上限" in failed["failure_message"]
            assert len(calls) == 2

            clock.now = 60
            retry = client.post(f"/api/inputs/{denied.json()['id']}/retry")
            assert retry.status_code == 202
            assert len(calls) == 3
            reprocess = client.post(f"/api/items/{item['id']}/reprocess")
            assert reprocess.status_code == 200
            assert len(calls) == 4
            limited = client.post(f"/api/items/{item['id']}/reprocess")
            assert limited.status_code == 429
            assert "临时使用上限" in limited.json()["detail"]
            assert len(calls) == 4

            provider.max_input_bytes = max(calls[:2])
            too_large = client.post(f"/api/items/{item['id']}/reprocess")
            assert too_large.status_code == 413
            assert "原始记录已保留" in too_large.json()["detail"]
            assert len(calls) == 4
            assert len(client.get(f"/api/items/{item['id']}").json()["inputs"]) == 2
    finally:
        asyncio.run(provider._client.aclose())


@pytest.mark.parametrize("name", [
    "AI_ADMISSION_WINDOW_SECONDS", "AI_ADMISSION_CALL_LIMIT",
    "AI_ADMISSION_CONCURRENT_LIMIT", "AI_PROVIDER_INPUT_MAX_BYTES",
    "ASR_ADMISSION_WINDOW_SECONDS", "ASR_ADMISSION_CALL_LIMIT",
    "ASR_ADMISSION_CONCURRENT_LIMIT",
])
@pytest.mark.parametrize("value", ["0", "invalid"])
def test_provider_admission_configuration_fails_clearly(
    monkeypatch, tmp_path, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_environment(tmp_path / "empty.env")
