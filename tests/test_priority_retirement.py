import json
from datetime import datetime, timezone

import httpx
import pytest

from app.services.ai import OpenAIResponsesAIService
from app.services.deepseek import DeepSeekProvider


@pytest.mark.parametrize("provider_type", [OpenAIResponsesAIService, DeepSeekProvider])
def test_ai_retirement_across_capture_update_reprocess_preserves_reminder(client_factory, provider_type):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        instructions = payload.get("instructions") or payload["messages"][0]["content"]
        assert "Omit importance and urgency" in instructions
        assert "conservative inference" not in instructions
        assert "Calibrate each field" not in instructions
        context = json.loads(payload.get("input") or payload["messages"][1]["content"])
        requests.append(context)
        if context['existing_item'] is not None:
            assert 'is_pinned' not in context['existing_item']
        # Deliberately non-compliant provider: retired fields must not reach storage.
        result = json.dumps({
            "fields": {"title": "交材料", "importance": "high", "urgency": "low",
                       "deadline": "2026-09-21", "extra_information": {"constraint": "关系到住宿安排"}},
            "evidence_fields": ["importance", "urgency", "deadline"],
            "reminder": {"intent": True, "temporal_expression": "过几天"},
        })
        body = {"output_text": result} if provider_type is OpenAIResponsesAIService else {
            "choices": [{"message": {"content": result}, "finish_reason": "stop"}],
        }
        return httpx.Response(200, json=body)

    provider = provider_type(
        api_url="https://example.test/v1", api_key="test", model="test-model",
        timeout_seconds=1, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = client_factory(provider)
    client.app.state.processor.now_provider = lambda: datetime(2026, 9, 20, tzinfo=timezone.utc)
    text = "明天交材料，关系到住宿安排，过几天别让我忘了跟进"
    assert client.post("/api/inputs", json={"original_text": text}).status_code == 202
    dashboard = client.get("/api/items").json()
    assert dashboard["needs_confirmation"] == []
    item = dashboard["sortable_items"][0]
    assert item["importance"] == item["urgency"] == "unknown"
    assert item["priority_score"] is None
    assert item["show_reminder_prompt"] is False
    assert item["reminder"]["status"] == "needs_confirmation"
    assert item["reminder"]["remind_at"] is None
    item_id, reminder_id = item["id"], item["reminder"]["id"]
    assert item['is_pinned'] is False
    assert client.patch(f'/api/items/{item_id}', json={'is_pinned': True}).status_code == 200
    # Compatibility: existing user/historical values remain intact, even when
    # the next extraction supplies supported but different legacy values.
    assert client.patch(f"/api/items/{item_id}", json={
        "importance": "low", "urgency": "medium", "confirmed_important_fields": True,
    }).status_code == 200
    assert client.post(f"/api/items/{item_id}/inputs", json={"original_text": text}).status_code == 202
    assert client.post(f"/api/items/{item_id}/reprocess").status_code == 200
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail['item']['is_pinned'] is True
    assert detail["item"]["importance"] == "low"
    assert detail["item"]["urgency"] == "medium"
    assert detail["item"]["deadline"] == "2026-09-21"
    assert detail["item"]["extra_information"] == {"constraint": "关系到住宿安排"}
    assert detail["reminder"]["id"] == reminder_id
    assert detail["reminder"]["status"] == "needs_confirmation"
    assert all(entry["original_text"] == text for entry in detail["inputs"])
    assert [request["operation"] for request in requests] == ["create", "update", "update"]
