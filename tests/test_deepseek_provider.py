from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.schemas import FailureType, ImportantField, PersonalItemPublic
from app.services.ai import (
    AIAPIError,
    AIConfigurationError,
    AIInvalidOutputError,
    AINetworkError,
    create_ai_service,
)
from app.services.deepseek import DeepSeekProvider


INVALID_FIELD_TYPE_OUTPUT = json.dumps(
    {
        "fields": {
            "title": "准备高数考试",
            "type": "study",
            "importance": "unknown",
            "urgency": "unknown",
            "status": "active",
            "extra_information": "应该是 JSON 对象",
        },
        "evidence_fields": [],
    },
    ensure_ascii=False,
)


def extraction_json(
    *,
    importance: str = "unknown",
    urgency: str = "unknown",
    estimated_time: Any = 60,
) -> str:
    return json.dumps(
        {
            "fields": {
                "title": "复习高数",
                "type": "study",
                "importance": importance,
                "urgency": urgency,
                "deadline": None,
                "estimated_time": estimated_time,
                "status": "active",
                "next_action": "复习第四章",
                "extra_information": {"chapter": "第四章"},
            },
            "evidence_fields": [],
        },
        ensure_ascii=False,
    )


def contextual_extraction_json(
    *,
    title: str,
    item_type: str,
    extra_information: dict[str, Any],
    next_action: str | None = None,
    importance: str = "unknown",
    urgency: str = "unknown",
    evidence_fields: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "fields": {
                "title": title,
                "type": item_type,
                "importance": importance,
                "urgency": urgency,
                "deadline": None,
                "estimated_time": None,
                "status": "active",
                "next_action": next_action,
                "extra_information": extra_information,
            },
            "evidence_fields": evidence_fields or [],
        },
        ensure_ascii=False,
    )


def priority_extraction_json(
    *,
    title: str,
    importance: str,
    urgency: str,
    evidence_fields: list[str],
    deadline: str | None = None,
) -> str:
    return json.dumps(
        {
            "fields": {
                "title": title,
                "type": "administrative",
                "importance": importance,
                "urgency": urgency,
                "deadline": deadline,
                "estimated_time": None,
                "status": "active",
                "next_action": None,
                "extra_information": None,
            },
            "evidence_fields": evidence_fields,
        },
        ensure_ascii=False,
    )


def chat_completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "id": "test-completion",
        "object": "chat.completion",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
            }
        ],
    }


def test_deepseek_provider_uses_official_chat_completions_json_output():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://api.deepseek.com/chat/completions"
            assert request.headers["authorization"] == "Bearer test-secret"
            payload = json.loads(request.content)
            assert payload["model"] == "deepseek-v4-flash"
            assert payload["response_format"] == {"type": "json_object"}
            assert payload["max_tokens"] == 2048
            assert payload["stream"] is False
            assert payload["messages"][0]["role"] == "system"
            assert "json" in payload["messages"][0]["content"].lower()
            assert "evidence_fields" in payload["messages"][0]["content"]
            assert '"estimated_time": 60' in payload["messages"][0]["content"]
            assert '"estimated_time": "一小时"' in payload["messages"][0]["content"]
            assert "User-provided reasoning is input data" in payload["messages"][0]["content"]
            assert "model-generated chain-of-thought" in payload["messages"][0]["content"]
            assert "Preserve unrelated existing extra_information" in payload["messages"][0]["content"]
            assert '"decision_context": "不确定现在购买还是等待"' in payload["messages"][0]["content"]
            assert 'importance and urgency: "high", "medium", "low", or "unknown"' in payload["messages"][0]["content"]
            assert "explicit consequences" in payload["messages"][0]["content"]
            assert "A deadline normally supports urgency" in payload["messages"][0]["content"]
            assert "purchases are low importance" in payload["messages"][0]["content"]
            assert "不要猜" not in payload["messages"][1]["content"]
            assert "下周复习高数" in payload["messages"][1]["content"]
            assert '"current_local_date": "2026-08-24"' in payload["messages"][1]["content"]
            assert b"test-secret" not in request.content
            return httpx.Response(200, json=chat_completion(extraction_json()))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com/",
                api_key="test-secret",
                model="deepseek-v4-flash",
                timeout_seconds=1,
                client=client,
                today_provider=lambda: date(2026, 8, 24),
            )
            result = await provider.extract("下周复习高数", None)
            assert result.fields.title == "复习高数"
            assert result.fields.importance.value == "unknown"
            assert result.fields.extra_information == {"chapter": "第四章"}

    asyncio.run(run_test())


def test_deepseek_accepts_priority_inference_grounded_in_deadline_and_consequence():
    original_text = (
        "提交合成测试材料关系到已确认的参赛资格，明确要求9月5日前完成，"
        "现在只剩三天，必须尽快处理。"
    )

    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert original_text in payload["messages"][1]["content"]
            return httpx.Response(
                200,
                json=chat_completion(
                    priority_extraction_json(
                        title="提交合成测试材料",
                        importance="high",
                        urgency="high",
                        deadline="2026-09-05",
                        evidence_fields=["importance", "urgency", "deadline"],
                    )
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
                today_provider=lambda: date(2026, 9, 2),
            )
            result = await provider.extract(original_text, None)
            assert result.fields.importance.value == "high"
            assert result.fields.urgency.value == "high"
            assert result.fields.deadline == date(2026, 9, 5)
            assert result.evidence_fields == {
                ImportantField.IMPORTANCE,
                ImportantField.URGENCY,
                ImportantField.DEADLINE,
            }

    asyncio.run(run_test())


@pytest.mark.parametrize(
    "original_text",
    [
        "有空了解一种新工具。",
        "准备复习一门课程。",
        "想买一本书。",
    ],
)
def test_deepseek_keeps_priority_unknown_without_user_evidence(original_text):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=chat_completion(
                    priority_extraction_json(
                        title="记录一个待了解事项",
                        importance="unknown",
                        urgency="unknown",
                        evidence_fields=[],
                    )
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            result = await provider.extract(original_text, None)
            assert result.fields.importance.value == "unknown"
            assert result.fields.urgency.value == "unknown"
            assert result.evidence_fields == set()

    asyncio.run(run_test())


def test_deepseek_preserves_purchase_reasoning_concerns_and_constraints():
    original_text = (
        "我想买一个阅读设备，不算特别急，但担心之后涨价，不知道现在买还是等，"
        "预算充足，购买时间不会影响正常安排。"
    )
    expected_context = {
        "decision_context": "不确定现在购买还是等待",
        "concerns": ["担心之后涨价"],
        "constraints": ["预算充足", "购买时间不会影响正常安排"],
    }

    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert original_text in payload["messages"][1]["content"]
            return httpx.Response(
                200,
                json=chat_completion(
                    contextual_extraction_json(
                        title="购买阅读设备",
                        item_type="purchase_decision",
                        extra_information=expected_context,
                        urgency="low",
                        evidence_fields=["urgency"],
                    )
                ),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            result = await provider.extract(original_text, None)
            assert result.fields.extra_information == expected_context
            assert result.fields.next_action is None
            assert result.fields.importance.value == "unknown"
            assert result.fields.urgency.value == "low"
            assert result.evidence_fields == {ImportantField.URGENCY}

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("original_text", "title", "item_type", "expected_context", "next_action"),
    [
        pytest.param(
            "准备选一台便携阅读器，在候选甲和候选乙之间比较，出发五天前要定下来，"
            "已经筛出两个候选，接下来查看测评再决定。",
            "选择便携阅读器",
            "purchase_decision",
            {
                "options": ["候选甲", "候选乙"],
                "candidate_progress": "已经筛出两个候选",
                "intended_use": "出行时阅读",
                "timing_constraint": "出发五天前定下来",
            },
            "查看两个候选的测评再决定",
            id="candidate-options",
        ),
        pytest.param(
            "社团展示需要一个个人与AI协作的开源项目，要找时间讨论选哪个示例，"
            "目前倾向候选甲，但可能需要微调，不确定能否按时做完，希望先产出可展示版本。",
            "准备社团展示项目",
            "project_decision",
            {
                "purpose": "社团项目展示",
                "requirement": "个人与AI协作的开源项目",
                "preferred_candidate": "候选甲",
                "possible_changes": "可能需要一些微调",
                "uncertainty": "不确定能否按时完成",
                "goal": "产出一个可展示版本",
            },
            "找时间讨论选择哪个示例项目",
            id="project-background-uncertainty",
        ),
        pytest.param(
            "统计课程第二单元已经复习完，我觉得核心概念掌握得还可以，接下来复习第三单元。",
            "复习统计课程",
            "study",
            {
                "current_progress": "第二单元已经复习完成",
                "current_understanding": "第二单元核心概念掌握得还可以",
            },
            "复习第三单元",
            id="current-progress",
        ),
    ],
)
def test_deepseek_preserves_context_and_explicit_next_actions(
    original_text, title, item_type, expected_context, next_action
):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=chat_completion(
                    contextual_extraction_json(
                        title=title,
                        item_type=item_type,
                        extra_information=expected_context,
                        next_action=next_action,
                    )
                ),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            result = await provider.extract(original_text, None)
            assert result.fields.extra_information == expected_context
            assert result.fields.next_action == next_action
            assert result.fields.importance.value == "unknown"
            assert result.fields.urgency.value == "unknown"
            assert result.evidence_fields == set()

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("raw_estimated_time", "expected_minutes"),
    [
        ("一小时", 60),
        ("半小时", 30),
        ("2小时", 120),
        ("无法判断", None),
    ],
)
def test_deepseek_normalizes_natural_language_estimated_time(
    raw_estimated_time, expected_minutes
):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=chat_completion(
                    extraction_json(estimated_time=raw_estimated_time)
                ),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            result = await provider.extract("测试预计耗时", None)
            assert result.fields.estimated_time == expected_minutes

    asyncio.run(run_test())


def test_unknown_duration_does_not_clear_existing_estimate_on_update():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=chat_completion(
                    json.dumps(
                        {
                            "fields": {"estimated_time": "无法判断"},
                            "evidence_fields": [],
                        },
                        ensure_ascii=False,
                    )
                ),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            now = datetime.now(timezone.utc)
            existing = PersonalItemPublic(
                id=1,
                title="已有事项",
                type="study",
                importance="unknown",
                urgency="unknown",
                deadline=None,
                estimated_time=45,
                status="active",
                next_action=None,
                extra_information=None,
                created_time=now,
                updated_time=now,
            )
            result = await provider.extract("现在无法判断耗时", existing)
            assert "estimated_time" not in result.fields.model_fields_set

    asyncio.run(run_test())


def test_deepseek_normalizes_empty_optional_values_without_erasing_updates():
    async def run_test():
        responses = [
            json.dumps(
                {
                    "fields": {
                        "title": "整理课程资料",
                        "type": "study",
                        "importance": "unknown",
                        "urgency": "unknown",
                        "deadline": "",
                        "estimated_time": "",
                        "status": "active",
                        "next_action": "",
                        "extra_information": "",
                    },
                    "evidence_fields": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "fields": {
                        "deadline": "",
                        "estimated_time": "",
                        "next_action": "",
                        "extra_information": "",
                    },
                    "evidence_fields": [],
                },
                ensure_ascii=False,
            ),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            response = responses[call_index]
            call_index += 1
            return httpx.Response(200, json=chat_completion(response))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            created = await provider.extract("整理课程资料", None)
            assert created.fields.deadline is None
            assert created.fields.estimated_time is None
            assert created.fields.next_action is None
            assert created.fields.extra_information is None

            now = datetime.now(timezone.utc)
            existing = PersonalItemPublic(
                id=1,
                title="整理课程资料",
                type="study",
                importance="unknown",
                urgency="unknown",
                deadline=None,
                estimated_time=45,
                status="active",
                next_action="归类讲义",
                extra_information={"source": "课堂"},
                created_time=now,
                updated_time=now,
            )
            updated = await provider.extract("没有新增内容", existing)
            assert updated.fields.model_fields_set == set()

    asyncio.run(run_test())


def test_deepseek_invalid_output_gets_exactly_one_successful_repair():
    async def run_test():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = json.loads(request.content)
            if calls == 1:
                assert len(payload["messages"]) == 2
                return httpx.Response(200, json=chat_completion('{"fields":'))
            assert len(payload["messages"]) == 4
            assert "Validation error" in payload["messages"][-1]["content"]
            return httpx.Response(200, json=chat_completion(extraction_json()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            result = await provider.extract("复习课程", None)
            assert result.fields.title == "复习高数"
            assert calls == 2

    asyncio.run(run_test())


def test_deepseek_stops_after_one_failed_repair_attempt():
    async def run_test():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=chat_completion("not json"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            with pytest.raises(AIInvalidOutputError):
                await provider.extract("复习课程", None)
            assert calls == 2

    asyncio.run(run_test())


def deadline_extraction_json(
    deadline: str | None,
    *,
    extra_information: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "fields": {
                "title": "提交合成测试材料",
                "type": "project",
                "importance": "unknown",
                "urgency": "unknown",
                "deadline": deadline,
                "estimated_time": None,
                "status": "active",
                "next_action": None,
                "extra_information": extra_information,
            },
            "evidence_fields": ["deadline"],
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("today", "original_text", "model_deadline", "expected_deadline"),
    [
        (
            date(2026, 8, 24),
            "请在10月15日前提交合成测试材料",
            "2025-10-15",
            date(2026, 10, 15),
        ),
        (
            date(2026, 12, 20),
            "计划在1月5日前完成合成测试材料",
            "2026-01-05",
            date(2027, 1, 5),
        ),
        (
            date(2026, 8, 24),
            "明天提交合成测试材料",
            "2025-08-25",
            date(2026, 8, 25),
        ),
        (
            date(2026, 8, 24),
            "下周三提交合成测试材料",
            "2025-09-02",
            date(2026, 9, 2),
        ),
        (
            date(2026, 8, 24),
            "明年6月15日提交合成测试材料",
            "2026-06-15",
            date(2027, 6, 15),
        ),
        (
            date(2026, 8, 24),
            "明确在2025年10月15日完成历史记录",
            "2026-10-15",
            date(2025, 10, 15),
        ),
    ],
)
def test_deepseek_grounds_exact_dates_against_runtime_date(
    today, original_text, model_deadline, expected_deadline
):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert f'"current_local_date": "{today.isoformat()}"' in payload["messages"][1]["content"]
            return httpx.Response(
                200,
                json=chat_completion(deadline_extraction_json(model_deadline)),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
                today_provider=lambda: today,
            )
            result = await provider.extract(original_text, None)
            assert result.fields.deadline == expected_deadline

    asyncio.run(run_test())


def test_deepseek_preserves_ambiguous_month_without_inventing_day():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=chat_completion(deadline_extraction_json("2027-06-01")),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
                today_provider=lambda: date(2026, 8, 24),
            )
            result = await provider.extract("明年6月完成一次合成测试", None)
            assert result.fields.deadline is None
            assert result.fields.extra_information == {"date_context": "明年6月"}

    asyncio.run(run_test())


def test_deepseek_output_still_uses_existing_unknown_evidence_rule(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=chat_completion(
                extraction_json(importance="high", urgency="high")
            ),
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(
        api_url="https://api.deepseek.com",
        api_key="test-secret",
        model="deepseek-v4-flash",
        timeout_seconds=1,
        client=async_client,
    )
    client = client_factory(provider)

    response = client.post(
        "/api/inputs", json={"original_text": "下周复习高数，没有说明优先级"}
    )
    assert response.status_code == 202
    item = client.get("/api/items").json()["needs_confirmation"][0]
    assert item["importance"] == "unknown"
    assert item["urgency"] == "unknown"

    asyncio.run(async_client.aclose())


@pytest.mark.parametrize(
    ("status_code", "expected_message"),
    [
        (401, "DEEPSEEK_API_KEY"),
        (402, "余额不足"),
        (429, "限流"),
        (500, "暂时不可用"),
    ],
)
def test_deepseek_provider_classifies_api_errors(status_code, expected_message):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                json={
                    "error": {
                        "code": "test_error",
                        "message": "provider detail",
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            with pytest.raises(AIAPIError) as error:
                await provider.extract("测试", None)
            assert error.value.category == FailureType.API
            assert expected_message in error.value.user_message

    asyncio.run(run_test())


@pytest.mark.parametrize(
    "response_body",
    [
        {"choices": []},
        chat_completion(""),
        chat_completion("not valid json"),
        chat_completion(extraction_json(importance="invented")),
        chat_completion(extraction_json(estimated_time=True)),
    ],
)
def test_deepseek_provider_rejects_invalid_output(response_body):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_body)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            with pytest.raises(AIInvalidOutputError) as error:
                await provider.extract("测试", None)
            assert error.value.category == FailureType.INVALID_OUTPUT

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("raw_output", "failure_kind", "error_path", "error_type"),
    [
        ('{"fields":', "json_syntax", "line 1", "json_invalid"),
        (
            '{"evidence_fields": []}',
            "schema_validation",
            "fields",
            "missing",
        ),
        (
            INVALID_FIELD_TYPE_OUTPUT,
            "schema_validation",
            "fields.extra_information",
            "dict_type",
        ),
        (
            '{"fields": {"importance": "unknown", "urgency": "unknown"}, '
            '"evidence_fields": []}',
            "create_requirement",
            "fields.title",
            "missing",
        ),
    ],
)
def test_deepseek_debug_logs_raw_output_and_precise_validation_error(
    raw_output, failure_kind, error_path, error_type, caplog
):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=chat_completion(raw_output))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
                debug_output=True,
            )
            with pytest.raises(AIInvalidOutputError):
                await provider.extract("测试", None)

    caplog.set_level(logging.ERROR, logger="app.services.deepseek")
    asyncio.run(run_test())
    log_text = caplog.text
    assert f"failure_kind={failure_kind}" in log_text
    assert error_path in log_text
    assert error_type in log_text
    assert raw_output in log_text
    assert "test-secret" not in log_text


def test_deepseek_debug_output_is_disabled_by_default(caplog):
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=chat_completion(INVALID_FIELD_TYPE_OUTPUT)
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            with pytest.raises(AIInvalidOutputError):
                await provider.extract("测试", None)

    caplog.set_level(logging.ERROR, logger="app.services.deepseek")
    asyncio.run(run_test())
    assert "AI_DEBUG_OUTPUT" not in caplog.text
    assert INVALID_FIELD_TYPE_OUTPUT not in caplog.text


def test_invalid_deepseek_output_never_creates_personal_item(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=chat_completion(INVALID_FIELD_TYPE_OUTPUT)
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(
        api_url="https://api.deepseek.com",
        api_key="test-secret",
        client=async_client,
        debug_output=True,
    )
    client = client_factory(provider)

    response = client.post(
        "/api/inputs", json={"original_text": "准备高数考试，预计一小时"}
    )
    assert response.status_code == 202
    dashboard = client.get("/api/items").json()
    assert dashboard["sortable_items"] == []
    assert dashboard["needs_confirmation"] == []
    assert dashboard["failed_inputs"][0]["failure_type"] == "invalid_output"
    user_id = client.get("/api/auth/me").json()["id"]
    assert client.app.state.repository.list_active_items(user_id) == []

    asyncio.run(async_client.aclose())


def test_deepseek_provider_classifies_network_errors():
    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_url="https://api.deepseek.com",
                api_key="test-secret",
                client=client,
            )
            with pytest.raises(AINetworkError) as error:
                await provider.extract("测试", None)
            assert error.value.category == FailureType.NETWORK
            assert "DeepSeek" in error.value.user_message

    asyncio.run(run_test())


@pytest.mark.parametrize("retired_model", ["deepseek-chat", "deepseek-reasoner"])
def test_deepseek_provider_rejects_retired_aliases(retired_model):
    with pytest.raises(AIConfigurationError) as error:
        DeepSeekProvider(
            api_url="https://api.deepseek.com",
            api_key="test-secret",
            model=retired_model,
        )
    assert error.value.category == FailureType.CONFIGURATION
    assert "deepseek-v4-flash" in error.value.user_message


def test_service_factory_selects_deepseek_as_default(tmp_path):
    settings = Settings(
        database_path=tmp_path / "factory.db",
        deepseek_api_key="test-secret",
    )
    provider = create_ai_service(settings)
    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == "deepseek-v4-flash"
    assert provider.api_url == "https://api.deepseek.com/chat/completions"
    assert provider.debug_output is False
