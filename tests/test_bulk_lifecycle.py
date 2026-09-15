from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.auth_repository import AuthRepository
from app.database import Database
from app.reminder_repository import ReminderRepository
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.schemas import (
    AIExtraction,
    AIItemFields,
    BulkLifecycleAction,
    ImportantField,
    InputMethod,
    ItemStatus,
    MAX_BULK_LIFECYCLE_ITEMS,
)
from app.services.voice_deletions import VoiceDeletionLedger
from tests.conftest import FunctionAIService


COMPLETE_TIME = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)
TRASH_TIME = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
RESTORE_TIME = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
FUTURE = datetime(2035, 1, 1, tzinfo=timezone.utc)


def create_item(repository: Repository, user_id: int, title: str):
    item_input = repository.create_input(title, InputMethod.TEXT, user_id)
    assert repository.claim_input(item_input.id, user_id)
    return repository.create_item_from_input(
        item_input.id,
        user_id,
        AIItemFields(title=title, type="task", status=ItemStatus.ACTIVE),
        set(),
    )


@pytest.fixture
def bulk_context(tmp_path: Path):
    database = Database(tmp_path / "bulk.db")
    database.initialize()
    auth = AuthRepository(database)
    user_a = auth.create_user(
        email="bulk-a@example.com",
        password_hash="hash-a",
        display_name="Bulk A",
        timezone_name="Asia/Shanghai",
    )
    user_b = auth.create_user(
        email="bulk-b@example.com",
        password_hash="hash-b",
        display_name="Bulk B",
        timezone_name="Europe/London",
    )
    return database, Repository(database), ReminderRepository(database), user_a, user_b


def insert_lifecycle_items(
    database: Database,
    user_id: int,
    *,
    status: ItemStatus,
    count: int,
) -> list[int]:
    timestamp = COMPLETE_TIME.isoformat()
    completed_at = timestamp if status == ItemStatus.COMPLETED else None
    trashed_at = TRASH_TIME.isoformat() if status == ItemStatus.TRASH else None
    source = "active" if status == ItemStatus.TRASH else None
    item_ids: list[int] = []
    with database.transaction() as connection:
        for index in range(count):
            inserted = connection.execute(
                """
                INSERT INTO personal_items (
                    user_id, title, type, importance, urgency, status,
                    created_time, updated_time, completed_at, trashed_at,
                    status_before_trash
                ) VALUES (?, ?, 'task', 'medium', 'medium', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    f"capacity-{status.value}-{index:03d}",
                    status.value,
                    timestamp,
                    timestamp,
                    completed_at,
                    trashed_at,
                    source,
                ),
            )
            item_id = int(inserted.lastrowid)
            item_ids.append(item_id)
            connection.execute(
                """
                INSERT INTO item_inputs (
                    user_id, item_id, original_text, input_method,
                    processing_status, created_time
                ) VALUES (?, ?, ?, 'text', 'succeeded', ?)
                """,
                (user_id, item_id, f"original-{item_id}", timestamp),
            )
    return item_ids


@pytest.mark.parametrize(
    ("item_status", "action"),
    [
        (ItemStatus.ACTIVE, BulkLifecycleAction.COMPLETE),
        (ItemStatus.COMPLETED, BulkLifecycleAction.RESTORE_TO_CURRENT),
        (ItemStatus.TRASH, BulkLifecycleAction.PERMANENTLY_DELETE),
    ],
)
def test_101_items_are_paged_into_backend_accepted_atomic_snapshots(
    client_factory,
    item_status: ItemStatus,
    action: BulkLifecycleAction,
) -> None:
    client = client_factory(FunctionAIService(lambda _text, _existing: None))
    user_id = client.get("/api/auth/me").json()["id"]
    all_ids = insert_lifecycle_items(
        client.app.state.database,
        user_id,
        status=item_status,
        count=MAX_BULK_LIFECYCLE_ITEMS + 1,
    )
    if item_status == ItemStatus.ACTIVE:
        unknown_ids = all_ids[-41:]
        placeholders = ", ".join("?" for _ in unknown_ids)
        with client.app.state.database.transaction() as connection:
            connection.execute(
                f"UPDATE personal_items SET importance = 'unknown' "
                f"WHERE id IN ({placeholders})",
                unknown_ids,
            )

    first_page = client.get(
        "/api/items",
        params={"status": item_status.value, "page": 1},
    )
    assert first_page.status_code == 200
    body = first_page.json()
    visible = body["sortable_items"] + body["needs_confirmation"]
    visible_ids = [item["id"] for item in visible]
    assert body["page"] == 1
    assert body["page_size"] == MAX_BULK_LIFECYCLE_ITEMS
    assert body["total_items"] == MAX_BULK_LIFECYCLE_ITEMS + 1
    assert body["total_pages"] == 2
    assert len(visible_ids) == MAX_BULK_LIFECYCLE_ITEMS
    assert set(visible_ids).issubset(all_ids)

    second_page = client.get(
        "/api/items",
        params={"status": item_status.value, "page": 2},
    ).json()
    second_page_ids = [
        item["id"]
        for item in second_page["sortable_items"]
        + second_page["needs_confirmation"]
    ]
    assert second_page["page"] == 2
    assert second_page_ids == list(set(all_ids) - set(visible_ids))
    assert client.get(
        "/api/items",
        params={"status": item_status.value, "page": 999},
    ).json()["page"] == 2

    oversized = client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": all_ids, "action": action.value},
    )
    assert oversized.status_code == 422
    assert client.get(
        "/api/items",
        params={"status": item_status.value},
    ).json()["total_items"] == MAX_BULK_LIFECYCLE_ITEMS + 1

    accepted = client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": visible_ids, "action": action.value},
    )
    assert accepted.status_code == 200
    assert accepted.json()["affected_ids"] == visible_ids
    remaining = client.get(
        "/api/items",
        params={"status": item_status.value},
    ).json()
    assert remaining["total_items"] == 1


def test_batch_complete_deduplicates_then_restores_atomically_with_reminders(
    bulk_context,
) -> None:
    _, repository, reminders, user, _ = bulk_context
    items = [create_item(repository, user.id, title) for title in ("one", "two")]
    reminder_ids = [
        reminders.create_reminder(
            item_id=item.id,
            user_id=user.id,
            scheduled_timezone="Asia/Shanghai",
            remind_at=FUTURE,
        ).id
        for item in items
    ]

    affected = repository.bulk_lifecycle(
        user.id,
        [items[0].id, items[0].id, items[1].id],
        BulkLifecycleAction.COMPLETE,
        now_utc=COMPLETE_TIME,
    )

    assert affected == [items[0].id, items[1].id]
    for item, reminder_id in zip(items, reminder_ids, strict=True):
        completed = repository.get_item(item.id, user.id)
        assert completed.status == ItemStatus.COMPLETED
        assert completed.completed_at == COMPLETE_TIME
        assert reminders.get_reminder(reminder_id, user.id).cancel_reason.value == (
            "item_completed"
        )

    repository.bulk_lifecycle(
        user.id,
        [item.id for item in items],
        BulkLifecycleAction.RESTORE_TO_CURRENT,
        now_utc=RESTORE_TIME,
    )
    for item, reminder_id in zip(items, reminder_ids, strict=True):
        restored = repository.get_item(item.id, user.id)
        assert restored.status == ItemStatus.ACTIVE
        assert restored.completed_at is None
        assert restored.trashed_at is None
        assert restored.status_before_trash is None
        assert reminders.get_reminder(reminder_id, user.id).status.value == "cancelled"


def test_batch_trash_and_restore_handles_mixed_origin_metadata(
    bulk_context,
) -> None:
    database, repository, _, user, _ = bulk_context
    current = create_item(repository, user.id, "current")
    history = create_item(repository, user.id, "history")
    legacy = create_item(repository, user.id, "legacy")
    repository.bulk_lifecycle(
        user.id,
        [history.id],
        BulkLifecycleAction.COMPLETE,
        now_utc=COMPLETE_TIME,
    )
    repository.bulk_lifecycle(
        user.id,
        [current.id, history.id],
        BulkLifecycleAction.MOVE_TO_TRASH,
        now_utc=TRASH_TIME,
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'trash', completed_at = NULL, trashed_at = ?,
                status_before_trash = NULL
            WHERE id = ? AND user_id = ?
            """,
            (TRASH_TIME.isoformat(), legacy.id, user.id),
        )

    trashed_current = repository.get_item(current.id, user.id)
    trashed_history = repository.get_item(history.id, user.id)
    assert trashed_current.status_before_trash.value == "active"
    assert trashed_current.completed_at is None
    assert trashed_history.status_before_trash.value == "completed"
    assert trashed_history.completed_at == COMPLETE_TIME
    assert trashed_history.trashed_at == TRASH_TIME

    repository.bulk_lifecycle(
        user.id,
        [current.id, history.id, legacy.id],
        BulkLifecycleAction.RESTORE_FROM_TRASH,
        now_utc=RESTORE_TIME,
    )
    assert repository.get_item(current.id, user.id).status == ItemStatus.ACTIVE
    restored_history = repository.get_item(history.id, user.id)
    assert restored_history.status == ItemStatus.COMPLETED
    assert restored_history.completed_at == COMPLETE_TIME
    assert restored_history.trashed_at is None
    assert restored_history.status_before_trash is None
    assert repository.get_item(legacy.id, user.id).status == ItemStatus.ACTIVE


def test_bulk_validation_rejects_bad_state_missing_and_cross_user_without_changes(
    bulk_context,
) -> None:
    _, repository, _, user_a, user_b = bulk_context
    active = create_item(repository, user_a.id, "valid active")
    completed = create_item(repository, user_a.id, "already completed")
    foreign = create_item(repository, user_b.id, "foreign")
    repository.bulk_lifecycle(
        user_a.id,
        [completed.id],
        BulkLifecycleAction.COMPLETE,
        now_utc=COMPLETE_TIME,
    )

    with pytest.raises(InvalidOperationError, match="requires every item"):
        repository.bulk_lifecycle(
            user_a.id,
            [active.id, completed.id],
            BulkLifecycleAction.COMPLETE,
            now_utc=TRASH_TIME,
        )
    assert repository.get_item(active.id, user_a.id).status == ItemStatus.ACTIVE
    assert repository.get_item(completed.id, user_a.id).completed_at == COMPLETE_TIME

    for invalid_id in (999_999, foreign.id):
        with pytest.raises(NotFoundError, match="one or more"):
            repository.bulk_lifecycle(
                user_a.id,
                [active.id, invalid_id],
                BulkLifecycleAction.MOVE_TO_TRASH,
                now_utc=TRASH_TIME,
            )
        assert repository.get_item(active.id, user_a.id).status == ItemStatus.ACTIVE
    assert repository.get_item(foreign.id, user_b.id).status == ItemStatus.ACTIVE


def attach_voice(
    database: Database,
    repository: Repository,
    user_id: int,
    item_id: int,
    suffix: str,
) -> str:
    item_input = repository.list_inputs_for_item(item_id, user_id)[0]
    storage_key = f"voice/{suffix}.bin"
    timestamp = COMPLETE_TIME.isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, item_input_id, position, client_segment_id,
                storage_key, original_size_bytes, original_sha256,
                transcription_status, provider, model, provider_transcript,
                attempt_count, created_time, updated_time,
                transcription_finished_time
            ) VALUES (?, ?, 0, ?, ?, 10, ?, 'succeeded', 'alibaba',
                      'qwen-audio-3.0-asr-flash', 'text', 1, ?, ?, ?)
            """,
            (
                user_id,
                item_input.id,
                f"segment-{suffix}",
                storage_key,
                "0" * 64,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    return storage_key


def test_permanent_bulk_delete_uses_exact_confirmed_snapshot_and_voice_ledger(
    bulk_context,
) -> None:
    database, repository, _, user, _ = bulk_context
    first, second, later = [
        create_item(repository, user.id, title)
        for title in ("snapshot one", "snapshot two", "arrived later")
    ]
    keys = {
        attach_voice(database, repository, user.id, first.id, "snapshot-one"),
        attach_voice(database, repository, user.id, second.id, "snapshot-two"),
    }
    repository.bulk_lifecycle(
        user.id,
        [first.id, second.id, later.id],
        BulkLifecycleAction.MOVE_TO_TRASH,
        now_utc=TRASH_TIME,
    )
    confirmed_snapshot = [first.id, second.id]

    affected = repository.bulk_lifecycle(
        user.id,
        [first.id, first.id, second.id],
        BulkLifecycleAction.PERMANENTLY_DELETE,
        now_utc=RESTORE_TIME,
    )

    assert affected == confirmed_snapshot
    assert repository.get_item(later.id, user.id).status == ItemStatus.TRASH
    with database.connection() as connection:
        remaining_ids = {
            int(row[0]) for row in connection.execute("SELECT id FROM personal_items")
        }
        ledger = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT storage_key, reason FROM voice_file_deletions"
            )
        }
    assert first.id not in remaining_ids
    assert second.id not in remaining_ids
    assert later.id in remaining_ids
    assert ledger == {(key, "item_permanent_delete") for key in keys}


def test_permanent_bulk_delete_rolls_back_if_ledger_write_fails(
    bulk_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, _, user, _ = bulk_context
    items = [create_item(repository, user.id, title) for title in ("safe one", "safe two")]
    attach_voice(database, repository, user.id, items[0].id, "rollback")
    repository.bulk_lifecycle(
        user.id,
        [item.id for item in items],
        BulkLifecycleAction.MOVE_TO_TRASH,
        now_utc=TRASH_TIME,
    )

    def fail_ledger(*_args, **_kwargs):
        raise RuntimeError("simulated ledger failure")

    monkeypatch.setattr(VoiceDeletionLedger, "record", staticmethod(fail_ledger))
    with pytest.raises(RuntimeError, match="ledger failure"):
        repository.bulk_lifecycle(
            user.id,
            [item.id for item in items],
            BulkLifecycleAction.PERMANENTLY_DELETE,
            now_utc=RESTORE_TIME,
        )

    assert [repository.get_item(item.id, user.id).status for item in items] == [
        ItemStatus.TRASH,
        ItemStatus.TRASH,
    ]


def test_bulk_api_is_narrow_bounded_and_csrf_protected(client_factory) -> None:
    extraction = lambda text, existing: AIExtraction(
        fields=AIItemFields(
            title=text,
            type="task",
            importance="medium",
            urgency="medium",
            status="active",
        ),
        evidence_fields={ImportantField.IMPORTANCE, ImportantField.URGENCY},
    )
    client = client_factory(FunctionAIService(extraction))
    client.post("/api/inputs", json={"original_text": "bulk endpoint"})
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]

    response = client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": [item_id, item_id], "action": "complete"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "action": "complete",
        "affected_ids": [item_id],
    }
    assert client.get(f"/api/items/{item_id}").json()["item"]["completed_at"]
    assert client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": [], "action": "complete"},
    ).status_code == 422
    assert client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": list(range(1, 102)), "action": "complete"},
    ).status_code == 422
    assert client.post(
        "/api/items/bulk-lifecycle",
        json={"item_ids": [item_id], "action": "arbitrary_patch"},
    ).status_code == 422
    for invalid_id in (True, str(item_id), 0, -1):
        assert client.post(
            "/api/items/bulk-lifecycle",
            json={"item_ids": [invalid_id], "action": "complete"},
        ).status_code == 422

    csrf = client.headers.pop("X-CSRF-Token")
    try:
        assert client.post(
            "/api/items/bulk-lifecycle",
            json={"item_ids": [item_id], "action": "restore_to_current"},
        ).status_code == 403
    finally:
        client.headers["X-CSRF-Token"] = csrf
