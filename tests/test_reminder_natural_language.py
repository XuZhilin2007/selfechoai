from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    AIExtraction,
    AIItemFields,
    AIReminderCandidate,
    ImportantField,
)
from tests.conftest import FunctionAIService


UTC = timezone.utc
REFERENCE = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
FUTURE_REFERENCE = datetime(2099, 8, 27, 2, 0, tzinfo=UTC)


def extraction(
    title: str,
    *,
    reminder_intent: bool,
    temporal_expression: str | None,
    deadline: date | None = None,
) -> AIExtraction:
    return AIExtraction(
        fields=AIItemFields(
            title=title,
            type="other",
            importance="unknown",
            urgency="unknown",
            deadline=deadline,
            status="active",
        ),
        evidence_fields=(
            {ImportantField.DEADLINE} if deadline is not None else set()
        ),
        reminder=AIReminderCandidate(
            intent=reminder_intent,
            temporal_expression=temporal_expression,
        ),
    )


def captured_item(client, text: str):
    response = client.post("/api/inputs", json={"original_text": text})
    assert response.status_code == 202
    dashboard = client.get("/api/items").json()
    items = dashboard["sortable_items"] + dashboard["needs_confirmation"]
    assert len(items) == 1
    detail = client.get(f"/api/items/{items[0]['id']}").json()
    return response.json(), items[0], detail


def test_reminder_candidate_requires_a_real_boolean_intent():
    with pytest.raises(ValidationError):
        AIReminderCandidate(intent="true", temporal_expression="明天")


def test_clear_natural_language_reminder_is_parsed_and_persisted(client_factory):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: extraction(
                "继续高数",
                reminder_intent=True,
                temporal_expression="30分钟后",
            )
        )
    )
    client.app.state.processor.now_provider = lambda: FUTURE_REFERENCE

    captured, dashboard_item, detail = captured_item(
        client,
        "30 分钟后提醒我继续高数",
    )

    assert captured["original_text"] == "30 分钟后提醒我继续高数"
    assert detail["inputs"][0]["processing_status"] == "succeeded"
    assert detail["reminder"]["status"] == "scheduled"
    assert detail["reminder"]["source_expression"] == "30分钟后"
    assert datetime.fromisoformat(detail["reminder"]["remind_at"]) == datetime(
        2099, 8, 27, 2, 30, tzinfo=UTC
    )
    assert dashboard_item["show_reminder_prompt"] is False


def test_ambiguous_reminder_preserves_item_and_expression(client_factory):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: extraction(
                "问学长",
                reminder_intent=True,
                temporal_expression="过几天",
            )
        )
    )

    _, dashboard_item, detail = captured_item(client, "过几天提醒我问一下学长")

    assert detail["inputs"][0]["processing_status"] == "succeeded"
    assert detail["item"]["title"] == "问学长"
    assert detail["reminder"]["status"] == "needs_confirmation"
    assert detail["reminder"]["remind_at"] is None
    assert detail["reminder"]["source_expression"] == "过几天"
    assert dashboard_item["show_reminder_prompt"] is False


def test_deadline_fact_without_reminder_intent_creates_no_reminder(client_factory):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: extraction(
                "参加比赛",
                reminder_intent=False,
                temporal_expression=None,
                deadline=date(2026, 10, 15),
            )
        )
    )

    _, dashboard_item, detail = captured_item(client, "比赛10月15日截止")

    assert detail["item"]["deadline"] == "2026-10-15"
    assert detail["reminder"] is None
    assert dashboard_item["show_reminder_prompt"] is True


def test_explicit_deadline_and_reminder_remain_separate(client_factory):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: extraction(
                "参加比赛",
                reminder_intent=True,
                temporal_expression="10月14日",
                deadline=date(2026, 10, 15),
            )
        )
    )
    client.app.state.processor.now_provider = lambda: REFERENCE

    _, _, detail = captured_item(client, "比赛10月15日截止，10月14日提醒我")

    assert detail["item"]["deadline"] == "2026-10-15"
    assert detail["reminder"]["status"] == "scheduled"
    assert datetime.fromisoformat(detail["reminder"]["remind_at"]) == datetime(
        2026, 10, 14, 1, 0, tzinfo=UTC
    )


def test_existing_manual_reminder_wins_over_later_ai_candidate(client_factory):
    def handler(text, existing):
        return extraction(
            "已有手动提醒",
            reminder_intent=existing is not None,
            temporal_expression="明天" if existing is not None else None,
        )

    client = client_factory(FunctionAIService(handler))
    _, item, _ = captured_item(client, "先创建事项")
    manual = client.post(
        f"/api/items/{item['id']}/reminder",
        json={"local_date": "2099-12-30", "local_time": "08:30"},
    )
    assert manual.status_code == 201

    update = client.post(
        f"/api/items/{item['id']}/inputs",
        json={"original_text": "明天提醒我再看一次"},
    )
    assert update.status_code == 202
    detail = client.get(f"/api/items/{item['id']}").json()

    assert detail["reminder"]["id"] == manual.json()["id"]
    assert detail["reminder"]["remind_at"] == manual.json()["remind_at"]


def test_item_and_reminder_write_roll_back_together(client_factory, monkeypatch):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: extraction(
                "事务验收",
                reminder_intent=True,
                temporal_expression="15分钟后",
            )
        )
    )
    client.app.state.processor.now_provider = lambda: FUTURE_REFERENCE
    reminder_repository = client.app.state.processor.reminder_repository

    def fail_reminder_creation(*args, **kwargs):
        raise RuntimeError("forced reminder transaction failure")

    monkeypatch.setattr(
        reminder_repository,
        "create_ai_reminder_if_absent",
        fail_reminder_creation,
    )
    captured = client.post(
        "/api/inputs",
        json={"original_text": "15分钟后提醒我检查事务"},
    )

    assert captured.status_code == 202
    dashboard = client.get("/api/items").json()
    assert dashboard["sortable_items"] == []
    assert dashboard["needs_confirmation"] == []
    assert dashboard["failed_inputs"][0]["original_text"] == (
        "15分钟后提醒我检查事务"
    )
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM personal_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
