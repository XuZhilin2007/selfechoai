from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.auth_repository import AuthRepository
from app.database import Database
from app.repository import NotFoundError, Repository
from app.schemas import AIItemFields, BulkLifecycleAction, InputMethod, ItemStatus
from app.services.trash_retention import (
    TrashRetentionService,
    TrashRetentionSweepResult,
    run_trash_retention_worker,
)
from app.services.voice_deletions import VoiceDeletionLedger
from tests.conftest import FunctionAIService


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def create_item(repository: Repository, user_id: int, title: str):
    item_input = repository.create_input(title, InputMethod.TEXT, user_id)
    assert repository.claim_input(item_input.id, user_id)
    return repository.create_item_from_input(
        item_input.id,
        user_id,
        AIItemFields(title=title, type="task", status=ItemStatus.ACTIVE),
        set(),
    )


def set_lifecycle(
    database: Database,
    item_id: int,
    *,
    status: str,
    trashed_at: datetime | None = None,
    source: str | None = None,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE personal_items
            SET status = ?, trashed_at = ?, status_before_trash = ?,
                completed_at = CASE WHEN ? = 'completed' THEN completed_at ELSE NULL END
            WHERE id = ?
            """,
            (
                status,
                trashed_at.isoformat() if trashed_at is not None else None,
                source,
                status,
                item_id,
            ),
        )


@pytest.fixture
def retention_context(tmp_path: Path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    auth = AuthRepository(database)
    user_a = auth.create_user(
        email="retention-a@example.com",
        password_hash="hash-a",
        display_name="Retention A",
        timezone_name="Asia/Shanghai",
    )
    user_b = auth.create_user(
        email="retention-b@example.com",
        password_hash="hash-b",
        display_name="Retention B",
        timezone_name="Europe/London",
    )
    return database, Repository(database), user_a, user_b


def test_retention_uses_exact_30_day_trash_clock_and_ignores_other_states(
    retention_context,
) -> None:
    database, repository, user, _ = retention_context
    almost = create_item(repository, user.id, "29d 23:59:59")
    exact = create_item(repository, user.id, "exact cutoff")
    older = create_item(repository, user.id, "older")
    active = create_item(repository, user.id, "active")
    completed = create_item(repository, user.id, "completed")
    unknown = create_item(repository, user.id, "trash without clock")
    set_lifecycle(
        database,
        almost.id,
        status="trash",
        trashed_at=NOW - timedelta(days=30) + timedelta(seconds=1),
        source="active",
    )
    set_lifecycle(
        database,
        exact.id,
        status="trash",
        trashed_at=NOW - timedelta(days=30),
        source="active",
    )
    set_lifecycle(
        database,
        older.id,
        status="trash",
        trashed_at=NOW - timedelta(days=31),
        source="active",
    )
    set_lifecycle(
        database,
        active.id,
        status="active",
        trashed_at=NOW - timedelta(days=90),
    )
    set_lifecycle(
        database,
        completed.id,
        status="completed",
        trashed_at=NOW - timedelta(days=90),
    )
    set_lifecycle(database, unknown.id, status="trash", trashed_at=None)

    purged = repository.purge_expired_trash(now_utc=NOW, batch_size=20)

    assert purged == [older.id, exact.id]
    remaining = {item.id for item in repository.list_items_by_status(ItemStatus.TRASH, user.id)}
    assert remaining == {almost.id, unknown.id}
    assert repository.get_item(active.id, user.id).status == ItemStatus.ACTIVE
    assert repository.get_item(completed.id, user.id).status == ItemStatus.COMPLETED


def test_retention_is_bounded_repeatable_and_user_independent(
    retention_context,
) -> None:
    database, repository, user_a, user_b = retention_context
    expired_a = [create_item(repository, user_a.id, f"expired-{index}") for index in range(3)]
    expired_b = create_item(repository, user_b.id, "other user expired")
    protected_b = create_item(repository, user_b.id, "other user protected")
    for index, item in enumerate(expired_a):
        set_lifecycle(
            database,
            item.id,
            status="trash",
            trashed_at=NOW - timedelta(days=31, seconds=index),
            source="active",
        )
    set_lifecycle(
        database,
        expired_b.id,
        status="trash",
        trashed_at=NOW - timedelta(days=31),
        source="active",
    )
    set_lifecycle(
        database,
        protected_b.id,
        status="trash",
        trashed_at=NOW - timedelta(days=2),
        source="active",
    )

    first = repository.purge_expired_trash(now_utc=NOW, batch_size=2)
    second = repository.purge_expired_trash(now_utc=NOW, batch_size=2)
    third = repository.purge_expired_trash(now_utc=NOW, batch_size=2)
    fourth = repository.purge_expired_trash(now_utc=NOW, batch_size=2)

    assert len(first) == 2
    assert len(second) == 2
    assert third == []
    assert fourth == []
    assert set(first + second) == {item.id for item in expired_a} | {expired_b.id}
    assert repository.get_item(protected_b.id, user_b.id).status == ItemStatus.TRASH


def test_retention_query_uses_aligned_index_without_temporary_sort(
    retention_context,
) -> None:
    database, _, _, _ = retention_context
    with database.connection() as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM personal_items
            WHERE status = 'trash'
              AND trashed_at IS NOT NULL
              AND trashed_at <= ?
            ORDER BY trashed_at ASC, id ASC
            LIMIT ?
            """,
            ((NOW - timedelta(days=30)).isoformat(), 100),
        ).fetchall()

    details = "\n".join(str(row["detail"]) for row in plan).upper()
    assert "IDX_PERSONAL_ITEMS_TRASH_RETENTION" in details
    assert "TEMP B-TREE" not in details


def test_restore_before_sweep_prevents_deletion(retention_context) -> None:
    database, repository, user, _ = retention_context
    item = create_item(repository, user.id, "restore first")
    set_lifecycle(
        database,
        item.id,
        status="trash",
        trashed_at=NOW - timedelta(days=31),
        source="active",
    )
    repository.bulk_lifecycle(
        user.id,
        [item.id],
        BulkLifecycleAction.RESTORE_FROM_TRASH,
        now_utc=NOW,
    )

    assert repository.purge_expired_trash(now_utc=NOW, batch_size=10) == []
    assert repository.get_item(item.id, user.id).status == ItemStatus.ACTIVE


def test_restore_and_purge_race_serializes_without_resurrection(
    retention_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository, user, _ = retention_context
    item = create_item(repository, user.id, "racing item")
    set_lifecycle(
        database,
        item.id,
        status="trash",
        trashed_at=NOW - timedelta(days=31),
        source="active",
    )
    entered_delete = Event()
    allow_delete = Event()
    original_delete = Repository._permanently_delete_rows

    def paused_delete(connection, rows, *, deleted_time):
        if rows:
            entered_delete.set()
            assert allow_delete.wait(timeout=5)
        return original_delete(connection, rows, deleted_time=deleted_time)

    monkeypatch.setattr(
        Repository,
        "_permanently_delete_rows",
        staticmethod(paused_delete),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(
            repository.purge_expired_trash,
            now_utc=NOW,
            batch_size=10,
        )
        assert entered_delete.wait(timeout=5)
        restore_future = executor.submit(
            repository.bulk_lifecycle,
            user.id,
            [item.id],
            BulkLifecycleAction.RESTORE_FROM_TRASH,
            now_utc=NOW,
        )
        allow_delete.set()
        assert purge_future.result(timeout=5) == [item.id]
        with pytest.raises(NotFoundError, match="not found"):
            restore_future.result(timeout=5)

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = ?", (item.id,)
        ).fetchone()[0] == 0


class FakeVoiceStorage:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.deleted: list[str] = []

    def delete_original(self, storage_key: str) -> bool:
        if storage_key in self.fail:
            raise PermissionError("synthetic physical delete failure")
        self.deleted.append(storage_key)
        return True

    def cleanup_stale_parts(self, **_kwargs) -> int:
        return 0

    def cleanup_stale_tmp_files(self, **_kwargs) -> int:
        return 0


def attach_voice(
    database: Database,
    repository: Repository,
    user_id: int,
    item_id: int,
    suffix: str,
) -> str:
    item_input = repository.list_inputs_for_item(item_id, user_id)[0]
    storage_key = f"voice/{suffix}.bin"
    timestamp = NOW.isoformat()
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
                f"retention-{suffix}",
                storage_key,
                "0" * 64,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    return storage_key


def test_physical_voice_failure_leaves_committed_retryable_ledger(
    retention_context,
) -> None:
    database, repository, user, _ = retention_context
    item = create_item(repository, user.id, "voice failure")
    key = attach_voice(database, repository, user.id, item.id, "failure")
    set_lifecycle(
        database,
        item.id,
        status="trash",
        trashed_at=NOW - timedelta(days=31),
        source="active",
    )
    storage = FakeVoiceStorage(fail={key})
    service = TrashRetentionService(
        repository,
        VoiceDeletionLedger(database, storage),
    )

    result = service.run_sweep_once(now_utc=NOW, batch_size=10)

    assert result.purged_items == 1
    assert result.voice_failed == 1
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM personal_items WHERE id = ?", (item.id,)
        ).fetchone()[0] == 0
        ledger = connection.execute(
            """
            SELECT storage_key, reason, attempt_count, last_error
            FROM voice_file_deletions
            """
        ).fetchone()
    assert ledger["storage_key"] == key
    assert ledger["reason"] == "item_permanent_delete"
    assert ledger["attempt_count"] == 1
    assert "PermissionError" in ledger["last_error"]

    storage.fail.clear()
    retried = service.run_sweep_once(now_utc=NOW, batch_size=10)
    assert retried.purged_items == 0
    assert retried.voice_deleted_or_absent == 1
    assert storage.deleted == [key]
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_retention_batch_larger_than_legacy_voice_drain_limit_loses_no_cleanup(
    retention_context,
) -> None:
    database, repository, user, _ = retention_context
    keys: set[str] = set()
    for index in range(30):
        item = create_item(repository, user.id, f"voice-{index}")
        keys.add(attach_voice(database, repository, user.id, item.id, str(index)))
        set_lifecycle(
            database,
            item.id,
            status="trash",
            trashed_at=NOW - timedelta(days=31),
            source="active",
        )
    storage = FakeVoiceStorage()
    service = TrashRetentionService(
        repository,
        VoiceDeletionLedger(database, storage),
    )

    result = service.run_sweep_once(now_utc=NOW, batch_size=40)

    assert result.purged_items == 30
    assert result.voice_selected == 30
    assert result.voice_deleted_or_absent == 30
    assert set(storage.deleted) == keys
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM voice_file_deletions"
        ).fetchone()[0] == 0


def test_worker_survives_one_exception_without_logging_user_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    completed = asyncio.Event()

    class FlakyService:
        def __init__(self) -> None:
            self.calls = 0

        def run_sweep_once(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("PRIVATE USER TITLE MUST NOT BE LOGGED")
            completed.set()
            return TrashRetentionSweepResult(purged_items=0)

    async def exercise() -> int:
        service = FlakyService()
        settings = SimpleNamespace(
            trash_retention_batch_size=10,
            trash_retention_poll_interval_seconds=0.001,
        )
        task = asyncio.create_task(run_trash_retention_worker(service, settings))
        await asyncio.wait_for(completed.wait(), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return service.calls

    with caplog.at_level(logging.ERROR, logger="app.services.trash_retention"):
        calls = asyncio.run(exercise())

    assert calls >= 2
    assert "exception_type=RuntimeError" in caplog.text
    assert "PRIVATE USER TITLE" not in caplog.text


def test_normal_application_lifespan_starts_retention_worker(client_factory) -> None:
    client = client_factory(FunctionAIService(lambda _text, _existing: None))

    task = client.app.state.trash_retention_worker_task
    assert task is not None
    assert not task.done()
    assert client.app.state.trash_retention_service.repository is (
        client.app.state.repository
    )
