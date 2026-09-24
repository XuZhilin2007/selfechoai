from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from app.priority import rank_items
from app.schemas import AIExtraction, AIItemFields, PersonalItemPublic
from tests.conftest import FunctionAIService


NOW = datetime.fromisoformat("2026-09-20T04:00:00+00:00")  # Shanghai noon


def item(item_id, deadline=None, *, created=NOW, **values):
    return PersonalItemPublic.model_validate({
        "id": item_id, "title": str(item_id), "type": "other",
        "importance": "unknown", "urgency": "unknown", "deadline": deadline,
        "estimated_time": None, "status": "active", "next_action": None,
        "extra_information": None, "created_time": created, "updated_time": NOW,
        **values,
    })


def ranked(items, zone="Asia/Shanghai", now=NOW):
    ordered, confirmation = rank_items(items, timezone_name=zone, now=now)
    assert confirmation == []
    assert all(row.priority_score is None for row in ordered)
    return [row.id for row in ordered]


def test_all_deadline_buckets_precision_and_creation_order():
    items = [
        item(1, "2026-09-20T11:59:59.999999"),
        item(2, "2026-09-20T11:00:00"),
        item(3, "2026-09-19T23:00:00"),
        item(4, "2026-09-19T08:00:00"),
        item(5, "2026-09-19", created=NOW + timedelta(seconds=1)),
        item(6, "2026-09-19"),
        item(7, "2026-09-18T23:59:59"),
        item(8, "2026-09-20T12:00:00"),  # equal now: not overdue
        item(9, "2026-09-20T13:00:00"),
        item(10, "2026-09-20", created=NOW + timedelta(seconds=1)),
        item(11, "2026-09-20"),
        item(12, "2026-09-21T00:00:00"),
        item(13, "2026-09-21T09:00:00"),
        item(14, "2026-09-21"),
        item(15, "2026-09-22T00:00:00"),
        item(16, created=NOW + timedelta(seconds=1)),
        item(17),
    ]
    assert ranked(items[::-1]) == list(range(1, 18))


def test_priority_and_updated_time_do_not_change_order_or_group():
    items = [item(1, created=NOW - timedelta(days=1), importance="high", urgency="high"),
             item(2, importance="low", urgency="low"),
             item(3, created=NOW + timedelta(seconds=1))]
    assert ranked(items) == [3, 2, 1]
    changed = [row.model_copy(update={"updated_time": NOW + timedelta(days=100 - row.id),
                                    "importance": "high", "urgency": "high"}) for row in items]
    assert ranked(changed) == [3, 2, 1]


@pytest.mark.parametrize("deadline", [None, "2026-09-19", "2026-09-21", "2026-09-19T09:00:00", "2026-09-21T09:00:00"])
def test_stable_id_breaks_ties_independent_of_input_order(deadline):
    for rows in permutations([item(3, deadline), item(1, deadline), item(2, deadline)]):
        assert ranked(list(rows)) == [1, 2, 3]


def test_equal_timed_values_use_id_not_creation_time():
    assert ranked([item(2, "2026-09-21T09:00:00", created=NOW + timedelta(days=1)),
                   item(1, "2026-09-21T09:00:00")]) == [1, 2]


def test_aware_projection_naive_wall_and_floating_date():
    rows = [item(1, "2026-09-19T23:30:00Z"),  # Shanghai today 07:30, overdue
            item(2, "2026-09-20T08:00:00"),  # local wall, more recent overdue
            item(3, "2026-09-20"),
            item(4, "2026-09-20T04:00:00Z")]  # exactly now, timed before floating
    assert ranked(rows) == [2, 1, 4, 3]
    # Same data west of UTC: aware 1 is yesterday, naive 2 is future today.
    assert ranked(rows, "America/Los_Angeles") == [1, 4, 2, 3]


def test_dst_fold_classifies_by_instant_then_orders_by_local_calendar_clock():
    now = datetime.fromisoformat("2026-11-01T06:15:00Z")
    rows = [item(1, "2026-11-01T01:30:00-04:00"),
            item(2, "2026-11-01T01:30:00-05:00"),
            item(3, "2026-11-01T01:20:00"),
            item(4, "2026-11-01")]
    assert ranked(rows, "America/New_York", now) == [1, 3, 2, 4]


def test_dashboard_uses_current_profile_timezone_without_rewriting_deadlines(client_factory, monkeypatch):
    monkeypatch.setattr("app.main.utc_now", lambda: NOW)
    client = client_factory(FunctionAIService(lambda text, existing: AIExtraction(fields=AIItemFields(title=text))))
    values = {"aware": "2026-09-20T01:00:00Z", "naive": "2026-09-19T19:00:00"}
    for title in values:
        client.post("/api/inputs", json={"original_text": title})
    rows = client.get("/api/items").json()["sortable_items"]
    for row in rows:
        assert client.patch(f"/api/items/{row['id']}", json={
            "deadline": values[row["title"]], "confirmed_important_fields": True,
        }).status_code == 200
    user_id = client.get("/api/auth/me").json()["id"]
    for zone, expected in [("Asia/Shanghai", ["aware", "naive"]),
                           ("America/Los_Angeles", ["naive", "aware"])]:
        client.app.state.auth_repository.update_user_profile(user_id, timezone_name=zone)
        ranked_rows = client.get("/api/items").json()["sortable_items"]
        assert [row["title"] for row in ranked_rows] == expected
        assert {row["title"]: row["deadline"] for row in ranked_rows} == values


def test_api_ranks_before_pagination_uses_profile_and_ignores_reminders(client_factory, monkeypatch):
    monkeypatch.setattr("app.main.utc_now", lambda: NOW)
    client = client_factory(FunctionAIService(lambda text, existing: AIExtraction(fields=AIItemFields(title=text))))
    client.post("/api/inputs", json={"original_text": "seed"})
    user_id = client.get("/api/auth/me").json()["id"]
    database = client.app.state.database
    with database.transaction() as connection:
        seed = connection.execute("SELECT * FROM personal_items WHERE user_id = ?", (user_id,)).fetchone()
        columns = [key for key in seed.keys() if key != "id"]
        for index in range(104):
            values = dict(seed)
            values.update(title=str(index), created_time=(NOW + timedelta(seconds=index)).isoformat(),
                          updated_time=(NOW - timedelta(seconds=index)).isoformat(),
                          importance="high" if index % 2 else "unknown",
                          urgency="low", deadline=None)
            connection.execute(f"INSERT INTO personal_items ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                               [values[key] for key in columns])
        connection.execute("UPDATE personal_items SET deadline = ? WHERE id = ?",
                           ("2026-09-20T01:00:00Z", seed["id"]))
    client.app.state.auth_repository.update_user_profile(user_id, timezone_name="Asia/Shanghai")
    before = client.get("/api/items").json()
    assert before["needs_confirmation"] == []
    ids = [row["id"] for row in before["sortable_items"]]
    assert ids[0] == seed["id"]
    assert ids[1:] == sorted(ids[1:], reverse=True)
    # Reminder time cannot displace either no-deadline item or overdue item.
    response = client.post(f"/api/items/{ids[-1]}/reminder", json={"local_date": "2099-01-01"})
    assert response.status_code == 201
    assert [row["id"] for row in client.get("/api/items").json()["sortable_items"]] == ids
    pages = [client.get(f"/api/items?page={page}").json() for page in (1, 2)]
    assert [row["id"] for page in pages for row in page["sortable_items"]] == ids
    assert [len(page["sortable_items"]) for page in pages] == [100, 5]
    assert all(page["total_items"] == 105 and page["total_pages"] == 2 for page in pages)
    assert client.get("/api/items?page=999").json()["page"] == 2
