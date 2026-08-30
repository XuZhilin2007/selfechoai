from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.schemas import FailureType
from app.services.ai import (
    AIAPIError,
    AIInvalidOutputError,
    AINetworkError,
    OpenAIResponsesAIService,
)


@pytest.mark.parametrize("response_shape", ["direct", "rest"])
def test_openai_adapter_validates_structured_output(response_shape):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer secret"
            payload = json.loads(request.content)
            assert payload["store"] is False
            assert payload["text"]["format"]["type"] == "json_schema"
            assert "User-provided reasoning is input data" in payload["instructions"]
            assert "model-generated chain-of-thought" in payload["instructions"]
            assert "may be a decision, idea" in payload["instructions"]
            assert "exploration rather than a traditional todo" in payload["instructions"]
            assert "current_local_date" in payload["instructions"]
            assert "explicit consequences" in payload["instructions"]
            assert "Judge importance and urgency separately" in payload["instructions"]
            assert "generic category has a fixed priority" in payload["instructions"]
            assert "reminder.intent is true only" in payload["instructions"]
            assert "a date, deadline, class, exam" in payload["instructions"]
            assert "Never calculate remind_at" in payload["instructions"]
            schema = payload["text"]["format"]["schema"]
            assert "reminder" in schema["properties"]
            assert json.loads(payload["input"])["current_local_date"] == "2026-08-24"
            extraction = json.dumps(
                {
                    "fields": {
                        "title": "研究 AI Agent",
                        "type": "project",
                        "importance": "unknown",
                        "urgency": "unknown",
                        "status": "active",
                    },
                    "evidence_fields": [],
                    "reminder": {
                        "intent": False,
                        "temporal_expression": None,
                    },
                },
                ensure_ascii=False,
            )
            if response_shape == "direct":
                body = {"output_text": extraction}
            else:
                body = {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": extraction}],
                        }
                    ]
                }
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
                today_provider=lambda: date(2026, 8, 24),
            )
            result = await service.extract("有空研究 AI Agent", None)
            assert result.fields.title == "研究 AI Agent"
            assert result.fields.importance.value == "unknown"
            assert result.reminder.intent is False

    import asyncio

    asyncio.run(run_test())


def test_openai_adapter_returns_provider_independent_reminder_candidate():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output_text": json.dumps(
                        {
                            "fields": {
                                "title": "继续学习",
                                "type": "study",
                                "importance": "unknown",
                                "urgency": "unknown",
                                "status": "active",
                            },
                            "evidence_fields": [],
                            "reminder": {
                                "intent": True,
                                "temporal_expression": "30分钟后",
                            },
                        },
                        ensure_ascii=False,
                    )
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
            )
            result = await service.extract("30分钟后提醒我继续学习", None)

        assert result.reminder.intent is True
        assert result.reminder.temporal_expression == "30分钟后"

    import asyncio

    asyncio.run(run_test())


def test_openai_adapter_rejects_malformed_reminder_candidate():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output_text": json.dumps(
                        {
                            "fields": {
                                "title": "无效提醒",
                                "type": "note",
                                "importance": "unknown",
                                "urgency": "unknown",
                                "status": "active",
                            },
                            "evidence_fields": [],
                            "reminder": {
                                "intent": "true",
                                "temporal_expression": "明天",
                            },
                        },
                        ensure_ascii=False,
                    )
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
            )
            with pytest.raises(AIInvalidOutputError):
                await service.extract("明天提醒我", None)

    import asyncio

    asyncio.run(run_test())


def test_openai_adapter_rejects_invalid_provider_data():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output_text": json.dumps(
                        {
                            "fields": {
                                "title": "无效数据",
                                "importance": "certainly-important",
                            },
                            "evidence_fields": ["importance"],
                        }
                    )
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
            )
            with pytest.raises(AIInvalidOutputError) as error:
                await service.extract("text", None)
            assert error.value.category == FailureType.INVALID_OUTPUT

    import asyncio

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("status_code", "expected_message"),
    [
        (401, "API Key"),
        (429, "额度"),
        (500, "暂时不可用"),
    ],
)
def test_openai_adapter_classifies_api_errors(status_code, expected_message):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                json={"error": {"code": "test_error", "message": "provider detail"}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
            )
            with pytest.raises(AIAPIError) as error:
                await service.extract("text", None)
            assert error.value.category == FailureType.API
            assert expected_message in error.value.user_message

    import asyncio

    asyncio.run(run_test())


def test_openai_adapter_classifies_network_errors():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service = OpenAIResponsesAIService(
                api_url="https://example.test/v1/responses",
                api_key="secret",
                model="test-model",
                timeout_seconds=1,
                client=client,
            )
            with pytest.raises(AINetworkError) as error:
                await service.extract("text", None)
            assert error.value.category == FailureType.NETWORK
            assert "网络" in error.value.user_message

    import asyncio

    asyncio.run(run_test())
