from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from app.database import Database
from app.schemas import (
    AIItemFields,
    BulkLifecycleAction,
    FailureType,
    ImportantField,
    InputMethod,
    ItemInputPublic,
    ItemStatus,
    MAX_BULK_LIFECYCLE_ITEMS,
    PersonalItemPublic,
    PriorityLevel,
    ProcessingStatus,
    ReminderCreationCandidate,
    UserItemPatch,
)
from app.services.voice_deletions import VoiceDeletionLedger
from app.time_utils import serialize_utc_datetime

if TYPE_CHECKING:
    from app.reminder_repository import ReminderRepository


class NotFoundError(Exception):
    pass


class InvalidOperationError(Exception):
    pass


MAX_TRASH_RETENTION_BATCH_SIZE = 500
TRASH_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class SystemPendingInput:
    input_id: int
    user_id: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cancel_active_reminders_for_item(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    user_id: int,
    cancel_reason: str,
    cancelled_time: str,
) -> None:
    connection.execute(
        """
        UPDATE reminders
        SET status = 'cancelled',
            cancelled_time = ?,
            cancel_reason = ?,
            updated_time = ?
        WHERE item_id = ? AND user_id = ?
          AND status IN ('needs_confirmation', 'scheduled')
        """,
        (
            cancelled_time,
            cancel_reason,
            cancelled_time,
            item_id,
            user_id,
        ),
    )


def _item_status_cancel_reason(status: str | None) -> str | None:
    if status == ItemStatus.COMPLETED.value:
        return "item_completed"
    if status == ItemStatus.TRASH.value:
        return "item_trashed"
    return None


def _serialize_deadline(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_deadline(value: str | None) -> date | datetime | None:
    if value is None:
        return None
    if "T" in value or " " in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.fromisoformat(value)


def _json_dump(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _lifecycle_transition_values(
    row: sqlite3.Row,
    action: BulkLifecycleAction,
    timestamp: str,
) -> dict[str, str | None]:
    """Return the complete lifecycle metadata update for one valid action."""

    current = str(row["status"])
    if action == BulkLifecycleAction.COMPLETE:
        if current != ItemStatus.ACTIVE.value:
            raise InvalidOperationError("complete requires every item to be active")
        return {
            "status": ItemStatus.COMPLETED.value,
            "completed_at": timestamp,
            "trashed_at": None,
            "status_before_trash": None,
        }
    if action == BulkLifecycleAction.RESTORE_TO_CURRENT:
        if current != ItemStatus.COMPLETED.value:
            raise InvalidOperationError(
                "restore_to_current requires every item to be completed"
            )
        return {
            "status": ItemStatus.ACTIVE.value,
            "completed_at": None,
            "trashed_at": None,
            "status_before_trash": None,
        }
    if action == BulkLifecycleAction.MOVE_TO_TRASH:
        if current not in {ItemStatus.ACTIVE.value, ItemStatus.COMPLETED.value}:
            raise InvalidOperationError(
                "move_to_trash requires every item to be active or completed"
            )
        return {
            "status": ItemStatus.TRASH.value,
            "completed_at": (
                row["completed_at"]
                if current == ItemStatus.COMPLETED.value
                else None
            ),
            "trashed_at": timestamp,
            "status_before_trash": current,
        }
    if action == BulkLifecycleAction.RESTORE_FROM_TRASH:
        if current != ItemStatus.TRASH.value:
            raise InvalidOperationError(
                "restore_from_trash requires every item to be in trash"
            )
        restore_completed = row["status_before_trash"] == ItemStatus.COMPLETED.value
        return {
            "status": (
                ItemStatus.COMPLETED.value
                if restore_completed
                else ItemStatus.ACTIVE.value
            ),
            "completed_at": row["completed_at"] if restore_completed else None,
            "trashed_at": None,
            "status_before_trash": None,
        }
    raise InvalidOperationError("unsupported lifecycle action")


def _status_transition_values(
    row: sqlite3.Row,
    target: ItemStatus,
    timestamp: str,
) -> dict[str, str | None]:
    current = ItemStatus(str(row["status"]))
    if current == target:
        return {}
    if target == ItemStatus.TRASH:
        return _lifecycle_transition_values(
            row,
            BulkLifecycleAction.MOVE_TO_TRASH,
            timestamp,
        )
    if target == ItemStatus.ACTIVE:
        action = (
            BulkLifecycleAction.RESTORE_FROM_TRASH
            if current == ItemStatus.TRASH
            else BulkLifecycleAction.RESTORE_TO_CURRENT
        )
        return _lifecycle_transition_values(row, action, timestamp)
    if target == ItemStatus.COMPLETED:
        action = (
            BulkLifecycleAction.RESTORE_FROM_TRASH
            if current == ItemStatus.TRASH
            else BulkLifecycleAction.COMPLETE
        )
        values = _lifecycle_transition_values(row, action, timestamp)
        if values["status"] != ItemStatus.COMPLETED.value:
            raise InvalidOperationError(
                "an item trashed from current cannot restore directly to history"
            )
        return values
    raise InvalidOperationError("unsupported item status")


class Repository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> PersonalItemPublic:
        return PersonalItemPublic(
            id=row["id"],
            title=row["title"],
            type=row["type"],
            importance=row["importance"],
            urgency=row["urgency"],
            deadline=_parse_deadline(row["deadline"]),
            estimated_time=row["estimated_time"],
            status=row["status"],
            next_action=row["next_action"],
            extra_information=(
                json.loads(row["extra_information"])
                if row["extra_information"] is not None
                else None
            ),
            completed_at=_parse_optional_datetime(row["completed_at"]),
            trashed_at=_parse_optional_datetime(row["trashed_at"]),
            status_before_trash=row["status_before_trash"],
            # Historical v7 migration consumers have no Pin column; startup
            # still requires v8 for the application itself.
            is_pinned=bool(row["is_pinned"]) if "is_pinned" in row.keys() else False,
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
        )

    @staticmethod
    def _input_from_row(
        row: sqlite3.Row,
        voice_segment_ids: list[int] | None = None,
    ) -> ItemInputPublic:
        return ItemInputPublic(
            id=row["id"],
            item_id=row["item_id"],
            original_text=row["original_text"],
            input_method=row["input_method"],
            processing_status=row["processing_status"],
            failure_type=row["failure_type"],
            failure_message=row["failure_message"],
            created_time=datetime.fromisoformat(row["created_time"]),
            voice_segment_ids=voice_segment_ids or [],
        )

    @staticmethod
    def _voice_segment_ids(
        connection: sqlite3.Connection,
        input_id: int,
        user_id: int,
    ) -> list[int]:
        return [
            int(row["id"])
            for row in connection.execute(
                """
                SELECT id FROM voice_segments
                WHERE item_input_id = ? AND user_id = ?
                ORDER BY position ASC, id ASC
                """,
                (input_id, user_id),
            )
        ]

    def create_input(
        self,
        original_text: str,
        input_method: InputMethod,
        user_id: int,
        item_id: int | None = None,
    ) -> ItemInputPublic:
        now = utc_now().isoformat()
        with self.database.transaction() as connection:
            user_exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user_exists is None:
                raise NotFoundError("user not found")
            if item_id is not None:
                exists = connection.execute(
                    """
                    SELECT 1 FROM personal_items
                    WHERE id = ? AND user_id = ?
                    """,
                    (item_id, user_id),
                ).fetchone()
                if exists is None:
                    raise NotFoundError("item not found")
            cursor = connection.execute(
                """
                INSERT INTO item_inputs (
                    user_id, item_id, original_text, input_method,
                    processing_status, created_time
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    item_id,
                    original_text,
                    input_method.value,
                    ProcessingStatus.PENDING.value,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (cursor.lastrowid, user_id),
            ).fetchone()
        return self._input_from_row(row)

    def get_input(self, input_id: int, user_id: int) -> ItemInputPublic:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("input not found")
            return self._input_from_row(
                row,
                self._voice_segment_ids(connection, input_id, user_id),
            )

    def list_inputs_for_item(
        self, item_id: int, user_id: int
    ) -> list[ItemInputPublic]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM item_inputs
                WHERE item_id = ? AND user_id = ?
                ORDER BY created_time ASC, id ASC
                """,
                (item_id, user_id),
            ).fetchall()
            return [
                self._input_from_row(
                    row,
                    self._voice_segment_ids(connection, int(row["id"]), user_id),
                )
                for row in rows
            ]

    def list_unlinked_inputs(
        self, statuses: Iterable[ProcessingStatus], user_id: int
    ) -> list[ItemInputPublic]:
        status_values = [status.value for status in statuses]
        placeholders = ",".join("?" for _ in status_values)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM item_inputs
                WHERE user_id = ?
                  AND item_id IS NULL
                  AND processing_status IN ({placeholders})
                ORDER BY created_time DESC, id DESC
                """,
                [user_id, *status_values],
            ).fetchall()
            return [
                self._input_from_row(
                    row,
                    self._voice_segment_ids(connection, int(row["id"]), user_id),
                )
                for row in rows
            ]

    def system_list_pending_inputs(self) -> list[SystemPendingInput]:
        """Return all queued inputs for trusted startup recovery only."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id FROM item_inputs
                WHERE processing_status = 'pending'
                ORDER BY created_time ASC, id ASC
                """
            ).fetchall()
        return [
            SystemPendingInput(input_id=row["id"], user_id=row["user_id"])
            for row in rows
        ]

    def system_recover_interrupted_inputs(self) -> None:
        """Reset interrupted work across users during trusted app startup."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE item_inputs SET processing_status = 'pending'
                WHERE processing_status = 'processing'
                """
            )

    def claim_input(self, input_id: int, user_id: int) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE item_inputs
                SET processing_status = 'processing',
                    failure_type = NULL,
                    failure_message = NULL
                WHERE id = ? AND user_id = ? AND processing_status = 'pending'
                """,
                (input_id, user_id),
            )
        return cursor.rowcount == 1

    def mark_input_failed(
        self,
        input_id: int,
        user_id: int,
        failure_type: FailureType,
        failure_message: str,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE item_inputs
                SET processing_status = 'failed',
                    failure_type = ?,
                    failure_message = ?
                WHERE id = ? AND user_id = ? AND processing_status = 'processing'
                """,
                (failure_type.value, failure_message[:500], input_id, user_id),
            )

    def retry_input(self, input_id: int, user_id: int) -> ItemInputPublic:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("input not found")
            if row["processing_status"] != ProcessingStatus.FAILED.value:
                raise InvalidOperationError("only failed inputs can be retried")
            connection.execute(
                """
                UPDATE item_inputs
                SET processing_status = 'pending',
                    failure_type = NULL,
                    failure_message = NULL
                WHERE id = ? AND user_id = ?
                """,
                (input_id, user_id),
            )
            updated = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            voice_segment_ids = self._voice_segment_ids(
                connection,
                input_id,
                user_id,
            )
        return self._input_from_row(updated, voice_segment_ids)

    def delete_failed_unlinked_input(self, input_id: int, user_id: int) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("input not found")
            if (
                row["item_id"] is not None
                or row["processing_status"] != ProcessingStatus.FAILED.value
            ):
                raise InvalidOperationError(
                    "only failed unlinked Captures can be deleted"
                )
            storage_keys = [
                str(segment["storage_key"])
                for segment in connection.execute(
                    """
                    SELECT storage_key FROM voice_segments
                    WHERE item_input_id = ? AND user_id = ?
                    """,
                    (input_id, user_id),
                )
            ]
            VoiceDeletionLedger.record(
                connection,
                storage_keys,
                "failed_input_delete",
            )
            connection.execute(
                "DELETE FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            )

    def get_item(self, item_id: int, user_id: int) -> PersonalItemPublic:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("item not found")
        return self._item_from_row(row)

    def list_items_by_status(
        self, status: ItemStatus, user_id: int
    ) -> list[PersonalItemPublic]:
        with self.database.connection() as connection:
            # completed_at is the only completion chronology. updated_time is
            # used solely to make the unknown legacy subset deterministic.
            order_by = {
                ItemStatus.ACTIVE: "id ASC",
                ItemStatus.COMPLETED: (
                    "CASE WHEN completed_at IS NULL THEN 1 ELSE 0 END ASC, "
                    "completed_at DESC, updated_time DESC, id DESC"
                ),
                ItemStatus.TRASH: (
                    "CASE WHEN trashed_at IS NULL THEN 1 ELSE 0 END ASC, "
                    "trashed_at DESC, id DESC"
                ),
            }[status]
            rows = connection.execute(
                f"""
                SELECT * FROM personal_items
                WHERE status = ? AND user_id = ?
                ORDER BY {order_by}
                """,
                (status.value, user_id),
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def list_active_items(self, user_id: int) -> list[PersonalItemPublic]:
        return self.list_items_by_status(ItemStatus.ACTIVE, user_id)

    def create_item_from_input(
        self,
        input_id: int,
        user_id: int,
        fields: AIItemFields,
        evidence_fields: set[ImportantField],
        *,
        reminder: ReminderCreationCandidate | None = None,
        reminder_repository: ReminderRepository | None = None,
    ) -> PersonalItemPublic:
        if fields.title is None:
            raise InvalidOperationError("AI result for a new item requires a title")

        now = serialize_utc_datetime(utc_now(), field_name="now")
        # Legacy columns remain, but AI no longer authors priority metadata.
        importance = PriorityLevel.UNKNOWN
        urgency = PriorityLevel.UNKNOWN
        deadline = (
            fields.deadline
            if ImportantField.DEADLINE in evidence_fields
            else None
        )

        with self.database.transaction() as connection:
            input_row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            if input_row is None:
                raise NotFoundError("input not found")
            if input_row["processing_status"] != ProcessingStatus.PROCESSING.value:
                raise InvalidOperationError("input is not being processed")
            if input_row["item_id"] is not None:
                raise InvalidOperationError("input is already linked to an item")
            input_user_id = int(input_row["user_id"])

            initial_status = fields.status or ItemStatus.ACTIVE
            completed_at = (
                now if initial_status == ItemStatus.COMPLETED else None
            )
            trashed_at = now if initial_status == ItemStatus.TRASH else None
            status_before_trash = (
                ItemStatus.ACTIVE.value
                if initial_status == ItemStatus.TRASH
                else None
            )
            cursor = connection.execute(
                """
                INSERT INTO personal_items (
                    user_id, title, type, importance, urgency, deadline,
                    estimated_time, status, next_action, extra_information,
                    completed_at, trashed_at, status_before_trash,
                    created_time, updated_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_user_id,
                    fields.title.strip(),
                    (fields.type or "other").strip(),
                    importance.value,
                    urgency.value,
                    _serialize_deadline(deadline),
                    fields.estimated_time,
                    initial_status.value,
                    fields.next_action,
                    _json_dump(fields.extra_information),
                    completed_at,
                    trashed_at,
                    status_before_trash,
                    now,
                    now,
                ),
            )
            item_id = int(cursor.lastrowid)
            if reminder is not None:
                if reminder_repository is None:
                    raise InvalidOperationError(
                        "reminder repository is required"
                    )
                reminder_repository.create_ai_reminder_if_absent(
                    connection,
                    item_id=item_id,
                    user_id=input_user_id,
                    reminder=reminder,
                    created_time=now,
                )
            connection.execute(
                """
                UPDATE item_inputs
                SET item_id = ?,
                    processing_status = 'succeeded',
                    failure_type = NULL,
                    failure_message = NULL
                WHERE id = ? AND user_id = ?
                """,
                (item_id, input_id, input_user_id),
            )
            item_row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, input_user_id),
            ).fetchone()
        return self._item_from_row(item_row)

    def apply_ai_update(
        self,
        input_id: int,
        item_id: int,
        user_id: int,
        fields: AIItemFields,
        evidence_fields: set[ImportantField],
        *,
        reminder: ReminderCreationCandidate | None = None,
        reminder_repository: ReminderRepository | None = None,
    ) -> PersonalItemPublic:
        now = serialize_utc_datetime(utc_now(), field_name="now")
        with self.database.transaction() as connection:
            item_row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if item_row is None:
                raise NotFoundError("item not found")
            input_row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            if input_row is None:
                raise NotFoundError("input not found")
            if input_row["processing_status"] != ProcessingStatus.PROCESSING.value:
                raise InvalidOperationError("input is not being processed")
            if input_row["item_id"] != item_id:
                raise InvalidOperationError("input is not linked to item")

            updates: dict[str, Any] = {}
            supplied = fields.model_fields_set

            for name in ("title", "type", "estimated_time", "next_action"):
                if name not in supplied:
                    continue
                value = getattr(fields, name)
                if name in {"title", "type"} and value is None:
                    continue
                updates[name] = value

            if "status" in supplied and fields.status is not None:
                updates.update(
                    _status_transition_values(item_row, fields.status, now)
                )

            if "deadline" in supplied and ImportantField.DEADLINE in evidence_fields:
                updates["deadline"] = _serialize_deadline(fields.deadline)

            if "extra_information" in supplied and fields.extra_information is not None:
                existing = (
                    json.loads(item_row["extra_information"])
                    if item_row["extra_information"] is not None
                    else {}
                )
                existing.update(fields.extra_information)
                updates["extra_information"] = _json_dump(existing)

            if updates:
                updates["updated_time"] = now
                assignments = ", ".join(f"{name} = ?" for name in updates)
                connection.execute(
                    f"""
                    UPDATE personal_items SET {assignments}
                    WHERE id = ? AND user_id = ?
                    """,
                    [*updates.values(), item_id, user_id],
                )
                cancel_reason = _item_status_cancel_reason(updates.get("status"))
                if cancel_reason is not None:
                    _cancel_active_reminders_for_item(
                        connection,
                        item_id=item_id,
                        user_id=user_id,
                        cancel_reason=cancel_reason,
                        cancelled_time=now,
                    )

            if reminder is not None:
                if reminder_repository is None:
                    raise InvalidOperationError(
                        "reminder repository is required"
                    )
                reminder_repository.create_ai_reminder_if_absent(
                    connection,
                    item_id=item_id,
                    user_id=user_id,
                    reminder=reminder,
                    created_time=now,
                )

            connection.execute(
                """
                UPDATE item_inputs
                SET processing_status = 'succeeded',
                    failure_type = NULL,
                    failure_message = NULL
                WHERE id = ? AND user_id = ? AND item_id = ?
                """,
                (input_id, user_id, item_id),
            )
            updated_row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return self._item_from_row(updated_row)

    def apply_reprocessed_fields(
        self,
        item_id: int,
        user_id: int,
        fields: AIItemFields,
        evidence_fields: set[ImportantField],
    ) -> PersonalItemPublic:
        """Merge a full-history extraction without silently replacing core choices."""

        now = utc_now().isoformat()
        with self.database.transaction() as connection:
            item_row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if item_row is None:
                raise NotFoundError("item not found")

            updates: dict[str, Any] = {}
            supplied = fields.model_fields_set

            # Reprocessing enriches AI-derived fields, but does not reinterpret
            # user edits, lifecycle state, or the item's identity.
            if "estimated_time" in supplied and fields.estimated_time is not None:
                updates["estimated_time"] = fields.estimated_time
            if "next_action" in supplied and fields.next_action is not None:
                updates["next_action"] = fields.next_action

            if (
                "deadline" in supplied
                and ImportantField.DEADLINE in evidence_fields
                and item_row["deadline"] is None
                and fields.deadline is not None
            ):
                updates["deadline"] = _serialize_deadline(fields.deadline)

            if "extra_information" in supplied and fields.extra_information is not None:
                existing = (
                    json.loads(item_row["extra_information"])
                    if item_row["extra_information"] is not None
                    else {}
                )
                existing.update(fields.extra_information)
                updates["extra_information"] = _json_dump(existing)

            if updates:
                updates["updated_time"] = now
                assignments = ", ".join(f"{name} = ?" for name in updates)
                connection.execute(
                    f"""
                    UPDATE personal_items SET {assignments}
                    WHERE id = ? AND user_id = ?
                    """,
                    [*updates.values(), item_id, user_id],
                )

            updated_row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return self._item_from_row(updated_row)

    def update_item(
        self, item_id: int, user_id: int, patch: UserItemPatch
    ) -> PersonalItemPublic:
        excluded = {"confirmed_important_fields"}
        supplied = patch.model_fields_set - excluded
        if not supplied:
            raise InvalidOperationError("no item fields were supplied")
        if {"status", "is_pinned"} <= supplied:
            raise InvalidOperationError(
                "status and is_pinned must be changed separately"
            )

        important = {"importance", "urgency", "deadline"}
        if supplied & important and not patch.confirmed_important_fields:
            raise InvalidOperationError(
                "importance, urgency and deadline changes require explicit confirmation"
            )

        values: dict[str, Any] = {}
        for name in supplied:
            if name == "status":
                continue
            value = getattr(patch, name)
            if isinstance(value, (PriorityLevel, ItemStatus)):
                value = value.value
            elif name == "deadline":
                value = _serialize_deadline(value)
            elif name == "extra_information":
                value = _json_dump(value)
            values[name] = value
        timestamp = serialize_utc_datetime(utc_now(), field_name="now")

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("item not found")
            if "is_pinned" in supplied and row["status"] != ItemStatus.ACTIVE.value:
                raise InvalidOperationError("only active items allow Pin changes")
            if "status" in supplied and patch.status is not None:
                values.update(
                    _status_transition_values(row, patch.status, timestamp)
                )
            values["updated_time"] = timestamp
            assignments = ", ".join(f"{name} = ?" for name in values)
            connection.execute(
                f"""
                UPDATE personal_items SET {assignments}
                WHERE id = ? AND user_id = ?
                """,
                [*values.values(), item_id, user_id],
            )
            cancel_reason = _item_status_cancel_reason(values.get("status"))
            if cancel_reason is not None:
                _cancel_active_reminders_for_item(
                    connection,
                    item_id=item_id,
                    user_id=user_id,
                    cancel_reason=cancel_reason,
                    cancelled_time=values["updated_time"],
                )
            row = connection.execute(
                "SELECT * FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return self._item_from_row(row)

    @staticmethod
    def _permanently_delete_rows(
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        deleted_time: str,
    ) -> None:
        if not rows:
            return
        if any(row["status"] != ItemStatus.TRASH.value for row in rows):
            raise InvalidOperationError(
                "an item must be in trash before permanent deletion"
            )
        item_ids = [int(row["id"]) for row in rows]
        placeholders = ", ".join("?" for _ in item_ids)
        storage_keys = [
            str(segment["storage_key"])
            for segment in connection.execute(
                f"""
                SELECT voice_segments.storage_key
                FROM voice_segments
                JOIN item_inputs
                  ON item_inputs.id = voice_segments.item_input_id
                 AND item_inputs.user_id = voice_segments.user_id
                WHERE item_inputs.item_id IN ({placeholders})
                """,
                item_ids,
            )
        ]
        # The durable ledger is written before cascading deletes remove the
        # only relational path to external Voice storage keys.
        VoiceDeletionLedger.record(
            connection,
            storage_keys,
            "item_permanent_delete",
            created_time=deleted_time,
        )
        deleted = connection.execute(
            f"DELETE FROM personal_items WHERE id IN ({placeholders})",
            item_ids,
        )
        if deleted.rowcount != len(item_ids):
            raise InvalidOperationError("not every item could be permanently deleted")

    @staticmethod
    def _normalize_bulk_ids(item_ids: Iterable[int]) -> list[int]:
        supplied = list(item_ids)
        if not supplied:
            raise InvalidOperationError("item_ids must not be empty")
        if len(supplied) > MAX_BULK_LIFECYCLE_ITEMS:
            raise InvalidOperationError(
                f"at most {MAX_BULK_LIFECYCLE_ITEMS} item IDs are allowed"
            )
        if any(
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id <= 0
            for item_id in supplied
        ):
            raise InvalidOperationError("item_ids must contain positive integers")
        return list(dict.fromkeys(supplied))

    def bulk_lifecycle(
        self,
        user_id: int,
        item_ids: Iterable[int],
        action: BulkLifecycleAction,
        *,
        now_utc: datetime | None = None,
    ) -> list[int]:
        """Validate an owned snapshot fully, then mutate it in one transaction."""

        unique_ids = self._normalize_bulk_ids(item_ids)
        action = BulkLifecycleAction(action)
        timestamp = serialize_utc_datetime(
            now_utc or utc_now(),
            field_name="now_utc",
        )
        placeholders = ", ".join("?" for _ in unique_ids)
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM personal_items
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                [user_id, *unique_ids],
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise NotFoundError("one or more items were not found")
            rows_by_id = {int(row["id"]): row for row in rows}
            ordered_rows = [rows_by_id[item_id] for item_id in unique_ids]

            if action == BulkLifecycleAction.PERMANENTLY_DELETE:
                if any(
                    row["status"] != ItemStatus.TRASH.value
                    for row in ordered_rows
                ):
                    raise InvalidOperationError(
                        "permanently_delete requires every item to be in trash"
                    )
                self._permanently_delete_rows(
                    connection,
                    ordered_rows,
                    deleted_time=timestamp,
                )
                return unique_ids

            # Build every transition before the first UPDATE. This makes a bad
            # source state fail the whole command without partial mutation.
            transitions = [
                _lifecycle_transition_values(row, action, timestamp)
                for row in ordered_rows
            ]
            for row, values in zip(ordered_rows, transitions, strict=True):
                connection.execute(
                    """
                    UPDATE personal_items
                    SET status = ?, completed_at = ?, trashed_at = ?,
                        status_before_trash = ?, updated_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        values["status"],
                        values["completed_at"],
                        values["trashed_at"],
                        values["status_before_trash"],
                        timestamp,
                        int(row["id"]),
                        user_id,
                    ),
                )
                cancel_reason = _item_status_cancel_reason(values["status"])
                if cancel_reason is not None:
                    _cancel_active_reminders_for_item(
                        connection,
                        item_id=int(row["id"]),
                        user_id=user_id,
                        cancel_reason=cancel_reason,
                        cancelled_time=timestamp,
                    )
        return unique_ids

    def permanently_delete_item(self, item_id: int, user_id: int) -> None:
        try:
            self.bulk_lifecycle(
                user_id,
                [item_id],
                BulkLifecycleAction.PERMANENTLY_DELETE,
            )
        except NotFoundError as exc:
            raise NotFoundError("item not found") from exc

    def purge_expired_trash(
        self,
        *,
        now_utc: datetime | None = None,
        batch_size: int = 100,
    ) -> list[int]:
        if not 0 < batch_size <= MAX_TRASH_RETENTION_BATCH_SIZE:
            raise ValueError(
                "batch_size must be between 1 and "
                f"{MAX_TRASH_RETENTION_BATCH_SIZE}"
            )
        moment = now_utc or utc_now()
        timestamp = serialize_utc_datetime(moment, field_name="now_utc")
        cutoff = serialize_utc_datetime(
            moment - timedelta(days=TRASH_RETENTION_DAYS),
            field_name="trash_cutoff",
        )
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM personal_items
                WHERE status = 'trash'
                  AND trashed_at IS NOT NULL
                  AND trashed_at <= ?
                ORDER BY trashed_at ASC, id ASC
                LIMIT ?
                """,
                (cutoff, batch_size),
            ).fetchall()
            self._permanently_delete_rows(
                connection,
                list(rows),
                deleted_time=timestamp,
            )
        return [int(row["id"]) for row in rows]
