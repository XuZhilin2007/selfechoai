from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.repository as repository_module
from app.auth_repository import AuthRepository
from app.database import Database
from app.reminder_repository import ReminderRepository
from app.repository import InvalidOperationError, Repository
from app.schemas import AIItemFields, InputMethod, ItemStatus, UserItemPatch


COMPLETED_TIME = datetime(2026, 9, 13, 1, 0, tzinfo=timezone.utc)
TRASHED_TIME = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
RESTORED_TIME = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
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
def lifecycle_context(tmp_path: Path):
    database = Database(tmp_path / "lifecycle.db")
    database.initialize()
    auth = AuthRepository(database)
    user = auth.create_user(
        email="lifecycle@example.com",
        password_hash="hash",
        display_name="Lifecycle",
        timezone_name="Asia/Shanghai",
    )
    repository = Repository(database)
    return database, repository, ReminderRepository(database), user


def set_repository_now(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr(repository_module, "utc_now", lambda: value)


def test_active_completed_active_preserves_metadata_and_reminder_contract(
    lifecycle_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, reminders, user = lifecycle_context
    item = create_item(repository, user.id, "finish me")
    reminder = reminders.create_reminder(
        item_id=item.id,
        user_id=user.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=FUTURE,
    )

    set_repository_now(monkeypatch, COMPLETED_TIME)
    completed = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.COMPLETED),
    )

    assert completed.status == ItemStatus.COMPLETED
    assert completed.completed_at == COMPLETED_TIME
    assert completed.trashed_at is None
    assert completed.status_before_trash is None
    cancelled = reminders.get_reminder(reminder.id, user.id)
    assert cancelled.status.value == "cancelled"
    assert cancelled.cancel_reason.value == "item_completed"

    set_repository_now(monkeypatch, RESTORED_TIME)
    restored = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert restored.status == ItemStatus.ACTIVE
    assert restored.completed_at is None
    assert restored.trashed_at is None
    assert restored.status_before_trash is None
    assert reminders.get_reminder(reminder.id, user.id).status.value == "cancelled"


def test_active_trash_restore_and_legacy_trash_fallback(
    lifecycle_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, reminders, user = lifecycle_context
    item = create_item(repository, user.id, "trash active")
    reminder = reminders.create_reminder(
        item_id=item.id,
        user_id=user.id,
        scheduled_timezone="Asia/Shanghai",
        remind_at=FUTURE,
    )
    set_repository_now(monkeypatch, TRASHED_TIME)
    trashed = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    assert trashed.status == ItemStatus.TRASH
    assert trashed.completed_at is None
    assert trashed.trashed_at == TRASHED_TIME
    assert trashed.status_before_trash.value == "active"
    assert reminders.get_reminder(reminder.id, user.id).cancel_reason.value == (
        "item_trashed"
    )

    restored = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert restored.status == ItemStatus.ACTIVE
    assert restored.completed_at is None
    assert restored.trashed_at is None
    assert restored.status_before_trash is None
    assert reminders.get_reminder(reminder.id, user.id).status.value == "cancelled"

    legacy = create_item(repository, user.id, "legacy trash")
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'trash', completed_at = NULL, trashed_at = ?,
                status_before_trash = NULL
            WHERE id = ?
            """,
            (TRASHED_TIME.isoformat(), legacy.id),
        )
    legacy_restored = repository.update_item(
        legacy.id,
        user.id,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert legacy_restored.status == ItemStatus.ACTIVE
    assert legacy_restored.completed_at is None
    assert legacy_restored.trashed_at is None
    assert legacy_restored.status_before_trash is None


def test_completed_trash_restore_preserves_real_completion_time(
    lifecycle_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, _, user = lifecycle_context
    item = create_item(repository, user.id, "completed trash")
    set_repository_now(monkeypatch, COMPLETED_TIME)
    repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.COMPLETED),
    )

    set_repository_now(monkeypatch, TRASHED_TIME)
    trashed = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    assert trashed.completed_at == COMPLETED_TIME
    assert trashed.trashed_at == TRASHED_TIME
    assert trashed.status_before_trash.value == "completed"

    set_repository_now(monkeypatch, RESTORED_TIME)
    restored = repository.update_item(
        item.id,
        user.id,
        # A Trash restore uses origin metadata, even though the compatibility
        # status patch asks to leave Trash through the active path.
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert restored.status == ItemStatus.COMPLETED
    assert restored.completed_at == COMPLETED_TIME
    assert restored.trashed_at is None
    assert restored.status_before_trash is None

    current = repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.ACTIVE),
    )
    assert current.status == ItemStatus.ACTIVE
    assert current.completed_at is None


def test_ai_status_update_uses_the_same_transition_engine(
    lifecycle_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, _, user = lifecycle_context
    item = create_item(repository, user.id, "AI lifecycle")
    item_input = repository.create_input(
        "this is done",
        InputMethod.TEXT,
        user.id,
        item_id=item.id,
    )
    assert repository.claim_input(item_input.id, user.id)
    set_repository_now(monkeypatch, COMPLETED_TIME)

    updated = repository.apply_ai_update(
        item_input.id,
        item.id,
        user.id,
        AIItemFields(status=ItemStatus.COMPLETED),
        set(),
    )

    assert updated.status == ItemStatus.COMPLETED
    assert updated.completed_at == COMPLETED_TIME
    assert updated.trashed_at is None
    assert updated.status_before_trash is None


def test_ai_created_non_active_items_start_with_truthful_lifecycle_metadata(
    lifecycle_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, _, user = lifecycle_context
    set_repository_now(monkeypatch, COMPLETED_TIME)
    completed_input = repository.create_input("already done", InputMethod.TEXT, user.id)
    assert repository.claim_input(completed_input.id, user.id)
    completed = repository.create_item_from_input(
        completed_input.id,
        user.id,
        AIItemFields(title="already done", type="task", status=ItemStatus.COMPLETED),
        set(),
    )
    assert completed.completed_at == COMPLETED_TIME
    assert completed.trashed_at is None

    trashed_input = repository.create_input("discard this", InputMethod.TEXT, user.id)
    assert repository.claim_input(trashed_input.id, user.id)
    trashed = repository.create_item_from_input(
        trashed_input.id,
        user.id,
        AIItemFields(title="discard this", type="task", status=ItemStatus.TRASH),
        set(),
    )
    assert trashed.completed_at is None
    assert trashed.trashed_at == COMPLETED_TIME
    assert trashed.status_before_trash.value == "active"


def test_permanent_delete_requires_trash_and_ledgers_voice_before_cascade(
    lifecycle_context,
) -> None:
    database, repository, _, user = lifecycle_context
    item = create_item(repository, user.id, "voice lifecycle")
    item_input = repository.list_inputs_for_item(item.id, user.id)[0]
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO voice_segments (
                user_id, item_input_id, position, client_segment_id,
                storage_key, original_size_bytes, original_sha256,
                transcription_status, provider, model, provider_transcript,
                attempt_count, created_time, updated_time,
                transcription_finished_time
            ) VALUES (?, ?, 0, 'lifecycle-segment', 'voice/lifecycle.bin',
                      10, ?, 'succeeded', 'alibaba',
                      'qwen-audio-3.0-asr-flash', 'text', 1, ?, ?, ?)
            """,
            (user.id, item_input.id, "0" * 64, *([COMPLETED_TIME.isoformat()] * 3)),
        )

    with pytest.raises(InvalidOperationError, match="trash"):
        repository.permanently_delete_item(item.id, user.id)
    repository.update_item(
        item.id,
        user.id,
        UserItemPatch(status=ItemStatus.TRASH),
    )
    repository.permanently_delete_item(item.id, user.id)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = ?", (item.id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM item_inputs WHERE id = ?", (item_input.id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE storage_key = ?",
            ("voice/lifecycle.bin",),
        ).fetchone()[0] == 0
        ledger = connection.execute(
            "SELECT storage_key, reason FROM voice_file_deletions"
        ).fetchone()
    assert tuple(ledger) == ("voice/lifecycle.bin", "item_permanent_delete")


def test_history_sorting_uses_real_completion_time_then_stable_legacy_fallback(
    lifecycle_context,
) -> None:
    database, repository, _, user = lifecycle_context
    newer = create_item(repository, user.id, "newer known")
    older = create_item(repository, user.id, "older known")
    legacy_older = create_item(repository, user.id, "legacy older")
    legacy_newer = create_item(repository, user.id, "legacy newer")
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'completed', completed_at = ?, updated_time = ?
            WHERE id = ?
            """,
            ("2026-09-12T00:00:00+00:00", "2026-01-01T00:00:00+00:00", newer.id),
        )
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'completed', completed_at = ?, updated_time = ?
            WHERE id = ?
            """,
            ("2026-09-10T00:00:00+00:00", "2026-12-01T00:00:00+00:00", older.id),
        )
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'completed', completed_at = NULL, updated_time = ?
            WHERE id = ?
            """,
            ("2026-01-01T00:00:00+00:00", legacy_older.id),
        )
        connection.execute(
            """
            UPDATE personal_items
            SET status = 'completed', completed_at = NULL, updated_time = ?
            WHERE id = ?
            """,
            ("2026-02-01T00:00:00+00:00", legacy_newer.id),
        )

    history = repository.list_items_by_status(ItemStatus.COMPLETED, user.id)

    assert [item.id for item in history] == [
        newer.id,
        older.id,
        legacy_newer.id,
        legacy_older.id,
    ]
    assert [item.completed_at for item in history[-2:]] == [None, None]
