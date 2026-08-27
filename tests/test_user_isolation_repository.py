from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.auth import hash_password
from app.auth_repository import AuthRepository
from app.database import Database
from app.repository import InvalidOperationError, NotFoundError, Repository
from app.schemas import (
    AIExtraction,
    AIItemFields,
    FailureType,
    InputMethod,
    ItemStatus,
    ProcessingStatus,
    UserItemPatch,
)
from app.services.ai import AINetworkError, AIService
from app.services.processing import InputProcessingService


class SuccessfulAIService(AIService):
    async def extract(self, original_text, existing_item):
        return AIExtraction(
            fields=AIItemFields(
                title=f"已整理：{original_text}",
                type="note",
                status=ItemStatus.ACTIVE,
            ),
            evidence_fields=set(),
        )


class FailingAIService(AIService):
    async def extract(self, original_text, existing_item):
        raise AINetworkError("test network failure")


@pytest.fixture
def ownership_context(tmp_path: Path):
    database = Database(tmp_path / "ownership.db")
    database.initialize()
    auth_repository = AuthRepository(database)
    user_a = auth_repository.create_user(
        email="user-a@example.com",
        password_hash=hash_password("user A test password"),
        display_name="User A",
        timezone_name="Asia/Shanghai",
    )
    user_b = auth_repository.create_user(
        email="user-b@example.com",
        password_hash=hash_password("user B test password"),
        display_name="User B",
        timezone_name="Asia/Shanghai",
    )
    return database, Repository(database), user_a, user_b


def create_processed_item(
    repository: Repository,
    user_id: int,
    *,
    original_text: str,
    title: str,
):
    item_input = repository.create_input(
        original_text,
        InputMethod.TEXT,
        user_id,
    )
    assert repository.claim_input(item_input.id, user_id)
    item = repository.create_item_from_input(
        item_input.id,
        user_id,
        AIItemFields(title=title, type="note", status=ItemStatus.ACTIVE),
        set(),
    )
    return repository.get_input(item_input.id, user_id), item


def test_users_can_only_read_their_own_items_and_inputs(ownership_context):
    _, repository, user_a, user_b = ownership_context
    input_a, item_a = create_processed_item(
        repository,
        user_a.id,
        original_text="A 的原始输入",
        title="A 的事项",
    )
    input_b, item_b = create_processed_item(
        repository,
        user_b.id,
        original_text="B 的原始输入",
        title="B 的事项",
    )

    assert repository.get_item(item_a.id, user_a.id) == item_a
    assert repository.get_input(input_a.id, user_a.id) == input_a
    assert repository.list_active_items(user_a.id) == [item_a]
    assert repository.list_active_items(user_b.id) == [item_b]
    assert repository.list_inputs_for_item(item_a.id, user_a.id) == [input_a]
    assert repository.list_inputs_for_item(item_b.id, user_a.id) == []

    with pytest.raises(NotFoundError, match="item not found"):
        repository.get_item(item_b.id, user_a.id)
    with pytest.raises(NotFoundError, match="input not found"):
        repository.get_input(input_b.id, user_a.id)


def test_cross_user_update_delete_and_retry_look_like_not_found(ownership_context):
    _, repository, user_a, user_b = ownership_context
    _, item_b = create_processed_item(
        repository,
        user_b.id,
        original_text="B 的私有事项",
        title="B 的私有事项",
    )
    failed_input = repository.create_input(
        "B 的失败输入",
        InputMethod.TEXT,
        user_b.id,
    )
    assert repository.claim_input(failed_input.id, user_b.id)
    repository.mark_input_failed(
        failed_input.id,
        user_b.id,
        FailureType.NETWORK,
        "网络暂时不可用",
    )

    with pytest.raises(NotFoundError, match="item not found"):
        repository.update_item(
            item_b.id,
            user_a.id,
            UserItemPatch(title="越权修改"),
        )
    with pytest.raises(NotFoundError, match="item not found"):
        repository.permanently_delete_item(item_b.id, user_a.id)
    with pytest.raises(NotFoundError, match="input not found"):
        repository.retry_input(failed_input.id, user_a.id)

    assert repository.get_item(item_b.id, user_b.id).title == "B 的私有事项"
    assert repository.list_unlinked_inputs(
        [ProcessingStatus.FAILED], user_a.id
    ) == []
    assert repository.list_unlinked_inputs(
        [ProcessingStatus.FAILED], user_b.id
    )[0].id == failed_input.id
    retried = repository.retry_input(failed_input.id, user_b.id)
    assert retried.processing_status == ProcessingStatus.PENDING
    assert repository.list_unlinked_inputs(
        [ProcessingStatus.PENDING], user_a.id
    ) == []
    assert repository.list_unlinked_inputs(
        [ProcessingStatus.PENDING], user_b.id
    ) == [retried]


def test_input_cannot_be_linked_to_another_users_item(ownership_context):
    database, repository, user_a, user_b = ownership_context
    _, item_b = create_processed_item(
        repository,
        user_b.id,
        original_text="B 的事项来源",
        title="B 的事项",
    )

    with pytest.raises(NotFoundError, match="item not found"):
        repository.create_input(
            "A 试图补充 B 的事项",
            InputMethod.TEXT,
            user_a.id,
            item_id=item_b.id,
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO item_inputs (
                    user_id, item_id, original_text, input_method,
                    processing_status, created_time
                ) VALUES (?, ?, ?, 'text', 'pending', ?)
                """,
                (
                    user_a.id,
                    item_b.id,
                    "数据库也必须拒绝所有权不匹配",
                    "2026-08-25T00:00:00+00:00",
                ),
            )


def test_ai_update_requires_input_and_item_to_share_owner_and_link(ownership_context):
    _, repository, user_a, user_b = ownership_context
    _, item_a = create_processed_item(
        repository,
        user_a.id,
        original_text="A 的第一个事项",
        title="A1",
    )
    _, other_item_a = create_processed_item(
        repository,
        user_a.id,
        original_text="A 的第二个事项",
        title="A2",
    )
    _, item_b = create_processed_item(
        repository,
        user_b.id,
        original_text="B 的事项",
        title="B",
    )
    linked_input = repository.create_input(
        "A1 的补充",
        InputMethod.TEXT,
        user_a.id,
        item_id=item_a.id,
    )
    assert repository.claim_input(linked_input.id, user_a.id)
    update = AIItemFields(next_action="只应更新 A1")

    with pytest.raises(NotFoundError, match="item not found"):
        repository.apply_ai_update(
            linked_input.id,
            item_b.id,
            user_a.id,
            update,
            set(),
        )
    with pytest.raises(InvalidOperationError, match="not linked"):
        repository.apply_ai_update(
            linked_input.id,
            other_item_a.id,
            user_a.id,
            update,
            set(),
        )

    updated = repository.apply_ai_update(
        linked_input.id,
        item_a.id,
        user_a.id,
        update,
        set(),
    )
    assert updated.next_action == "只应更新 A1"


def test_reprocess_queries_are_scoped_to_the_owner(ownership_context):
    _, repository, user_a, user_b = ownership_context
    _, item_b = create_processed_item(
        repository,
        user_b.id,
        original_text="B 的完整历史",
        title="B 的事项",
    )
    processor = InputProcessingService(repository, SuccessfulAIService())

    with pytest.raises(NotFoundError, match="item not found"):
        asyncio.run(processor.reprocess_item(item_b.id, user_a.id))

    reprocessed = asyncio.run(processor.reprocess_item(item_b.id, user_b.id))
    assert reprocessed.id == item_b.id


def test_background_processing_preserves_owner_through_failure_and_retry(
    ownership_context,
):
    database, repository, user_a, user_b = ownership_context
    item_input = repository.create_input(
        "A 的后台处理输入",
        InputMethod.TEXT,
        user_a.id,
    )

    wrong_owner_processor = InputProcessingService(repository, SuccessfulAIService())
    asyncio.run(wrong_owner_processor.process_input(item_input.id, user_b.id))
    assert repository.get_input(
        item_input.id, user_a.id
    ).processing_status == ProcessingStatus.PENDING

    failing_processor = InputProcessingService(repository, FailingAIService())
    asyncio.run(failing_processor.process_input(item_input.id, user_a.id))
    failed = repository.get_input(item_input.id, user_a.id)
    assert failed.processing_status == ProcessingStatus.FAILED
    assert failed.original_text == "A 的后台处理输入"

    repository.retry_input(item_input.id, user_a.id)
    successful_processor = InputProcessingService(repository, SuccessfulAIService())
    asyncio.run(successful_processor.process_input(item_input.id, user_a.id))
    succeeded = repository.get_input(item_input.id, user_a.id)
    assert succeeded.processing_status == ProcessingStatus.SUCCEEDED
    assert succeeded.item_id is not None

    with database.connection() as connection:
        ownership = connection.execute(
            """
            SELECT input.user_id AS input_user_id,
                   item.user_id AS item_user_id
            FROM item_inputs AS input
            JOIN personal_items AS item ON item.id = input.item_id
            WHERE input.id = ?
            """,
            (item_input.id,),
        ).fetchone()
    assert ownership["input_user_id"] == user_a.id
    assert ownership["item_user_id"] == user_a.id


def test_system_pending_work_includes_explicit_owner(ownership_context):
    _, repository, user_a, user_b = ownership_context
    input_a = repository.create_input("等待处理 A", InputMethod.TEXT, user_a.id)
    input_b = repository.create_input("等待处理 B", InputMethod.TEXT, user_b.id)
    assert repository.claim_input(input_a.id, user_a.id)

    repository.system_recover_interrupted_inputs()

    pending = repository.system_list_pending_inputs()

    assert {(item.input_id, item.user_id) for item in pending} == {
        (input_a.id, user_a.id),
        (input_b.id, user_b.id),
    }
