from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.schemas import AIExtraction, AIItemFields, ItemStatus
from app.services.reminders import ReminderService
from tests.conftest import FunctionAIService


INVITE_CODE = "reminder-api-invite"
PASSWORD = "reminder API test password"


def reminder_ai(text, existing):
    return AIExtraction(
        fields=AIItemFields(
            title=text if existing is None else None,
            type="note" if existing is None else None,
            status=ItemStatus.ACTIVE if existing is None else None,
        ),
        evidence_fields=set(),
    )


def create_item(client: TestClient, title: str) -> int:
    response = client.post("/api/inputs", json={"original_text": title})
    assert response.status_code == 202
    dashboard = client.get("/api/items").json()
    items = dashboard["sortable_items"] + dashboard["needs_confirmation"]
    return next(item["id"] for item in items if item["title"] == title)


def local_date_after(timezone_name: str, days: int):
    return datetime.now(ZoneInfo(timezone_name)).date() + timedelta(days=days)


def expected_utc(local_date, local_time: str, timezone_name: str) -> datetime:
    local_value = datetime.combine(
        local_date,
        time.fromisoformat(local_time),
        tzinfo=ZoneInfo(timezone_name),
    )
    return local_value.astimezone(timezone.utc).replace(microsecond=0)


@pytest.fixture
def reminder_client(client_factory):
    return client_factory(FunctionAIService(reminder_ai))


def test_create_reschedule_cancel_and_default_time_conversion(reminder_client):
    client = reminder_client
    timezone_name = "Asia/Shanghai"
    first_item_id = create_item(client, "使用默认提醒时间")
    first_date = local_date_after(timezone_name, 2)

    created = client.post(
        f"/api/items/{first_item_id}/reminder",
        json={"local_date": first_date.isoformat()},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "scheduled"
    assert datetime.fromisoformat(created.json()["remind_at"]) == expected_utc(
        first_date,
        "09:00",
        timezone_name,
    )
    assert created.json()["scheduled_timezone"] == timezone_name

    settings = client.patch(
        "/api/reminder-settings",
        json={"default_reminder_time": "18:45"},
    )
    assert settings.status_code == 200
    assert settings.json() == {
        "timezone": timezone_name,
        "default_reminder_time": "18:45",
    }
    assert client.get("/api/reminder-settings").json() == settings.json()
    assert client.get("/api/auth/me").json()["default_reminder_time"] == "18:45"
    assert client.patch(
        "/api/reminder-settings",
        json={"default_reminder_time": "25:00"},
    ).status_code == 422
    assert client.get("/api/reminder-settings").json() == settings.json()

    second_item_id = create_item(client, "使用自定义默认时间")
    second_date = local_date_after(timezone_name, 3)
    second = client.post(
        f"/api/items/{second_item_id}/reminder",
        json={"local_date": second_date.isoformat()},
    )
    assert datetime.fromisoformat(second.json()["remind_at"]) == expected_utc(
        second_date,
        "18:45",
        timezone_name,
    )

    rescheduled_date = local_date_after(timezone_name, 4)
    rescheduled = client.put(
        f"/api/reminders/{created.json()['id']}",
        json={
            "local_date": rescheduled_date.isoformat(),
            "local_time": "15:30",
        },
    )
    assert rescheduled.status_code == 200
    assert datetime.fromisoformat(rescheduled.json()["remind_at"]) == expected_utc(
        rescheduled_date,
        "15:30",
        timezone_name,
    )

    cancelled = client.delete(f"/api/reminders/{created.json()['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_reason"] == "user_cancelled"


def test_invalid_or_past_manual_time_is_rejected_without_creating_reminder(
    reminder_client,
):
    client = reminder_client
    item_id = create_item(client, "拒绝过去时间")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    invalid_time = client.post(
        f"/api/items/{item_id}/reminder",
        json={"local_date": today.isoformat(), "local_time": "25:00"},
    )
    past = client.post(
        f"/api/items/{item_id}/reminder",
        json={
            "local_date": (today - timedelta(days=1)).isoformat(),
            "local_time": "09:00",
        },
    )

    assert invalid_time.status_code == 422
    assert past.status_code == 422
    state = client.get(f"/api/items/{item_id}/reminder").json()
    assert state["reminder"] is None
    assert state["show_reminder_prompt"] is True


def test_dst_gap_or_ambiguous_local_time_is_not_silently_guessed():
    with pytest.raises(ValueError, match="does not exist"):
        ReminderService.local_datetime_to_utc(
            date(2026, 3, 29),
            "01:30",
            "Europe/London",
        )
    with pytest.raises(ValueError, match="ambiguous"):
        ReminderService.local_datetime_to_utc(
            date(2026, 10, 25),
            "01:30",
            "Europe/London",
        )


def test_dashboard_lazy_due_transition_and_surface_are_idempotent(reminder_client):
    client = reminder_client
    due_item_id = create_item(client, "再次打开时带回视野")
    future_item_id = create_item(client, "未来仍保持 scheduled")
    future_date = local_date_after("Asia/Shanghai", 5)
    due_reminder = client.post(
        f"/api/items/{due_item_id}/reminder",
        json={"local_date": future_date.isoformat()},
    ).json()
    future_reminder = client.post(
        f"/api/items/{future_item_id}/reminder",
        json={"local_date": future_date.isoformat(), "local_time": "16:00"},
    ).json()

    past_value = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(
        microsecond=0
    )
    with client.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE reminders SET remind_at = ? WHERE id = ?",
            (past_value.isoformat(), due_reminder["id"]),
        )

    first_dashboard = client.get("/api/items")
    assert first_dashboard.status_code == 200
    assert [entry["id"] for entry in first_dashboard.json()["due_reminders"]] == [
        due_reminder["id"]
    ]
    with client.app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reminder_deliveries"
        ).fetchone()[0] == 0
    due_state = client.get(f"/api/items/{due_item_id}/reminder").json()[
        "reminder"
    ]
    future_state = client.get(f"/api/items/{future_item_id}/reminder").json()[
        "reminder"
    ]
    assert due_state["status"] == "due"
    assert future_state["status"] == "scheduled"

    due_time = due_state["due_time"]
    assert client.get("/api/items").json()["due_reminders"][0]["due_time"] == due_time
    assert len(client.get("/api/reminders/due").json()) == 1
    assert len(client.get("/api/reminders/upcoming").json()) == 1

    surfaced = client.post(f"/api/reminders/{due_reminder['id']}/surface")
    assert surfaced.status_code == 200
    assert surfaced.json()["surfaced_time"] is not None
    assert client.get(
        "/api/reminders/due", params={"unsurfaced_only": "true"}
    ).json() == []
    assert client.get("/api/items").json()["due_reminders"] == []
    assert client.get("/api/reminders/due").json()[0]["id"] == due_reminder["id"]
    assert future_state["id"] == future_reminder["id"]


def test_reminder_writes_require_csrf_without_affecting_authenticated_reads(
    reminder_client,
):
    client = reminder_client
    item_id = create_item(client, "提醒写入需要 CSRF")
    csrf_token = client.headers.pop("X-CSRF-Token")
    try:
        create_response = client.post(
            f"/api/items/{item_id}/reminder",
            json={
                "local_date": local_date_after("Asia/Shanghai", 2).isoformat(),
            },
        )
        settings_response = client.patch(
            "/api/reminder-settings",
            json={"default_reminder_time": "10:15"},
        )
        read_response = client.get(f"/api/items/{item_id}/reminder")
    finally:
        client.headers["X-CSRF-Token"] = csrf_token

    assert create_response.status_code == 403
    assert settings_response.status_code == 403
    assert read_response.status_code == 200
    assert read_response.json()["reminder"] is None


def test_prompt_dismissal_is_persistent_and_does_not_create_a_reminder(
    reminder_client,
):
    client = reminder_client
    item_id = create_item(client, "不需要提醒的事项")
    before = client.get("/api/items").json()
    item_before = next(
        item
        for item in before["needs_confirmation"] + before["sortable_items"]
        if item["id"] == item_id
    )
    assert item_before["show_reminder_prompt"] is True

    dismissed = client.post(f"/api/items/{item_id}/reminder-prompt/dismiss")

    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed_time"] is not None
    state = client.get(f"/api/items/{item_id}/reminder").json()
    assert state == {"reminder": None, "show_reminder_prompt": False}
    with client.app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reminders WHERE item_id = ?", (item_id,)
        ).fetchone()[0] == 0


def test_item_lifecycle_remains_the_only_reminder_lifecycle_truth(reminder_client):
    client = reminder_client
    item_id = create_item(client, "生命周期联动")
    reminder = client.post(
        f"/api/items/{item_id}/reminder",
        json={"local_date": local_date_after("Asia/Shanghai", 2).isoformat()},
    ).json()

    assert client.patch(
        f"/api/items/{item_id}", json={"status": "completed"}
    ).status_code == 200
    completed = client.get(f"/api/items/{item_id}/reminder").json()["reminder"]
    assert completed["status"] == "cancelled"
    assert completed["cancel_reason"] == "item_completed"

    assert client.patch(
        f"/api/items/{item_id}", json={"status": "active"}
    ).status_code == 200
    assert client.get(f"/api/items/{item_id}/reminder").json()["reminder"][
        "id"
    ] == reminder["id"]

    next_reminder = client.post(
        f"/api/items/{item_id}/reminder",
        json={"local_date": local_date_after("Asia/Shanghai", 3).isoformat()},
    ).json()
    assert client.patch(
        f"/api/items/{item_id}", json={"status": "trash"}
    ).status_code == 200
    trashed = client.get(f"/api/items/{item_id}/reminder").json()["reminder"]
    assert trashed["id"] == next_reminder["id"]
    assert trashed["cancel_reason"] == "item_trashed"

    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/items/{item_id}/reminder").status_code == 404


def register_user(client: TestClient, email: str, timezone_name: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": INVITE_CODE,
            "email": email,
            "password": PASSWORD,
            "display_name": email,
            "timezone": timezone_name,
        },
    )
    assert response.status_code == 201
    client.headers["X-CSRF-Token"] = client.cookies.get(CSRF_COOKIE_NAME)


def test_reminder_api_ownership_returns_404_for_cross_user_access(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "reminder-ownership.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE_CODE),
        app_origin="http://testserver",
        session_cookie_secure=False,
    )
    app = create_app(settings=settings, ai_service=FunctionAIService(reminder_ai))
    with TestClient(app) as user_a, TestClient(app) as user_b:
        register_user(user_a, "reminder-owner-a@example.com", "Asia/Shanghai")
        register_user(user_b, "reminder-owner-b@example.com", "Europe/London")
        item_b_id = create_item(user_b, "B 的私有提醒")
        reminder_b = user_b.post(
            f"/api/items/{item_b_id}/reminder",
            json={
                "local_date": local_date_after("Europe/London", 3).isoformat(),
            },
        ).json()

        assert user_a.get(f"/api/items/{item_b_id}/reminder").status_code == 404
        assert user_a.post(
            f"/api/items/{item_b_id}/reminder-prompt/dismiss"
        ).status_code == 404
        assert user_a.post(
            f"/api/items/{item_b_id}/reminder",
            json={"local_date": "2000-01-01", "local_time": "09:00"},
        ).status_code == 404
        assert user_a.put(
            f"/api/reminders/{reminder_b['id']}",
            json={
                "local_date": local_date_after("Asia/Shanghai", 4).isoformat(),
            },
        ).status_code == 404
        assert user_a.put(
            f"/api/reminders/{reminder_b['id']}",
            json={"local_date": "2000-01-01", "local_time": "09:00"},
        ).status_code == 404
        assert user_a.delete(f"/api/reminders/{reminder_b['id']}").status_code == 404
        assert user_a.post(
            f"/api/reminders/{reminder_b['id']}/surface"
        ).status_code == 404
        assert user_a.get("/api/reminders/upcoming").json() == []
        assert user_b.get("/api/reminders/upcoming").json()[0]["id"] == (
            reminder_b["id"]
        )
