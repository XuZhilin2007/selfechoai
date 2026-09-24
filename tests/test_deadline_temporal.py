from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest

from app.services.ai import OpenAIResponsesAIService
from app.services.deepseek import DeepSeekProvider
from app.schemas import AIExtraction, AIItemFields, UserItemPatch
from app.time_utils import deadline_is_overdue, deadline_local_value, user_local_date
from tests.conftest import FunctionAIService


@pytest.mark.parametrize("model", [AIItemFields, UserItemPatch])
@pytest.mark.parametrize("value", ["2026-09-20", "2026-09-20T00:00:00", "2026-09-20T00:00:00Z", "2026-09-20T00:00:00-07:00"])
def test_schema_keeps_midnight_precision_and_offset(model, value):
    deadline = model.model_validate({"deadline": value}).deadline
    assert isinstance(deadline, datetime) == ("T" in value)
    assert deadline.isoformat() == value.replace("Z", "+00:00")


@pytest.mark.parametrize("value,expected", [
    ("2000-01-01T00:00:00", "2026-09-21T00:00:00"),
    ("2026-09-20T23:30:00-07:00", "2026-09-20T23:30:00-07:00"),
])
def test_deepseek_date_grounding_preserves_timed_precision(value, expected):
    decoded = {"fields": {"deadline": value}, "evidence_fields": ["deadline"]}
    DeepSeekProvider._normalize_deadline(
        decoded, original_text="明天交材料", current_local_date=date(2026, 9, 20), is_update=False,
    )
    assert decoded["fields"]["deadline"] == expected


@pytest.mark.parametrize("value", ["2026-09-20", "2026-09-20T00:00:00", "2026-09-18T23:30:00+00:00"])
def test_deadline_round_trip_retains_stored_value_without_read_time_rewrite(client_factory, value):
    client = client_factory(FunctionAIService(lambda text, existing: AIExtraction(
        fields=AIItemFields(title=text, deadline=value), evidence_fields={"deadline"},
    )))
    assert client.post("/api/inputs", json={"original_text": "原始记录"}).status_code == 202
    item = client.get("/api/items").json()["sortable_items"][0]
    # Pydantic's public JSON spells UTC as Z; SQLite keeps its original ISO form.
    public_value = value.replace("+00:00", "Z")
    assert item["deadline"] == public_value
    assert client.get(f"/api/items/{item['id']}").json()["item"]["deadline"] == public_value
    with client.app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT deadline FROM personal_items WHERE id = ?", (item["id"],),
        ).fetchone()[0] == value


@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_floating_date_never_acquires_time_or_shifts(zone):
    value = date(2026, 9, 20)
    assert deadline_local_value(value, zone) is value
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    assert deadline_is_overdue(value, timezone_name=zone, now=now) == (
        value < user_local_date(now, zone)
    )


@pytest.mark.parametrize("value,expected,overdue", [
    ("2026-09-18", "2026-09-18", True),
    ("2026-09-19", "2026-09-19", False),
    ("2026-09-20", "2026-09-20", False),
    ("2026-09-18T23:30:00+00:00", "2026-09-19T07:30:00+08:00", True),
    ("2026-09-19T00:00:00+00:00", "2026-09-19T08:00:00+08:00", False),
    ("2026-09-19T00:30:00", "2026-09-19T00:30:00", True),
    ("2026-09-19T08:00:00", "2026-09-19T08:00:00", False),
    ("2026-09-19T08:00:00.000001", "2026-09-19T08:00:00.000001", False),
])
def test_deadline_projection_and_strict_overdue_boundary(value, expected, overdue):
    parsed = datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    assert deadline_local_value(parsed, "Asia/Shanghai").isoformat() == expected
    assert deadline_is_overdue(
        parsed, timezone_name="Asia/Shanghai",
        now=datetime(2026, 9, 19, tzinfo=timezone.utc),
    ) is overdue
    assert parsed.isoformat() == value


def test_aware_dst_fold_uses_instant_and_naive_retains_wall_convention():
    now = datetime.fromisoformat("2026-11-01T06:15:00+00:00")
    assert deadline_is_overdue(
        datetime.fromisoformat("2026-11-01T01:30:00-04:00"),
        timezone_name="America/New_York", now=now,
    )
    assert not deadline_is_overdue(
        datetime.fromisoformat("2026-11-01T01:30:00"),
        timezone_name="America/New_York", now=now,
    )


@pytest.mark.parametrize("provider_type", [OpenAIResponsesAIService, DeepSeekProvider])
def test_user_date_reaches_provider_on_capture_update_and_reprocess(client_factory, provider_type):
    observed = []

    def handler(request):
        payload = json.loads(request.content)
        context = json.loads(payload.get("input") or payload["messages"][1]["content"])
        local_date = date.fromisoformat(context["current_local_date"])
        observed.append(local_date)
        result = json.dumps({
            "fields": {"title": "明天交材料", "deadline": (local_date + timedelta(days=1)).isoformat()},
            "evidence_fields": ["deadline"],
            "reminder": {"intent": False, "temporal_expression": None},
        })
        body = {"output_text": result} if provider_type is OpenAIResponsesAIService else {
            "choices": [{"message": {"content": result}, "finish_reason": "stop"}],
        }
        return httpx.Response(200, json=body)

    provider = provider_type(
        api_url="https://example.test/v1", api_key="test", model="test-model",
        timeout_seconds=1, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    # Reuse the same provider across accounts; no shared mutable user clock.
    for zone, expected in [("Asia/Shanghai", date(2026, 9, 20)),
                           ("America/Los_Angeles", date(2026, 9, 19))]:
        client = client_factory(provider)
        user_id = client.get("/api/auth/me").json()["id"]
        client.app.state.auth_repository.update_user_profile(user_id, timezone_name=zone)
        client.app.state.processor.now_provider = lambda: datetime(2026, 9, 19, 16, 30, tzinfo=timezone.utc)
        assert client.post("/api/inputs", json={"original_text": "明天交材料"}).status_code == 202
        dashboard = client.get("/api/items").json()
        item = (dashboard["sortable_items"] + dashboard["needs_confirmation"])[0]
        assert item["deadline"] == (expected + timedelta(days=1)).isoformat()
        assert item["reminder"] is None
        assert item["show_reminder_prompt"] is False
        assert client.post(f"/api/items/{item['id']}/inputs", json={"original_text": "明天交材料"}).status_code == 202
        assert client.post(f"/api/items/{item['id']}/reprocess").status_code == 200
        assert observed[-3:] == [expected] * 3
