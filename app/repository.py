from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from app.database import Database
from app.schemas import (
    AIItemFields,
    FailureType,
    ImportantField,
    InputMethod,
    ItemInputPublic,
    ItemStatus,
    PersonalItemPublic,
    PriorityLevel,
    ProcessingStatus,
    ReminderCreationCandidate,
    UserItemPatch,
)
from app.services.voice_deletions import VoiceDeletionLedger

if TYPE_CHECKING:
    from app.reminder_repository import ReminderRepository


class NotFoundError(Exception):
    pass


class InvalidOperationError(Exception):
    pass


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
            rows = connection.execute(
                """
                SELECT * FROM personal_items
                WHERE status = ? AND user_id = ?
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

        now = utc_now().isoformat()
        importance = (
            fields.importance
            if ImportantField.IMPORTANCE in evidence_fields
            and fields.importance is not None
            else PriorityLevel.UNKNOWN
        )
        urgency = (
            fields.urgency
            if ImportantField.URGENCY in evidence_fields and fields.urgency is not None
            else PriorityLevel.UNKNOWN
        )
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

            cursor = connection.execute(
                """
                INSERT INTO personal_items (
                    user_id, title, type, importance, urgency, deadline,
                    estimated_time, status, next_action, extra_information,
                    created_time, updated_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_user_id,
                    fields.title.strip(),
                    (fields.type or "other").strip(),
                    importance.value,
                    urgency.value,
                    _serialize_deadline(deadline),
                    fields.estimated_time,
                    (fields.status or ItemStatus.ACTIVE).value,
                    fields.next_action,
                    _json_dump(fields.extra_information),
                    now,
                    now,
                ),
            )
            item_id = int(cursor.lastrowid)
            if reminder is not None:
                if reminder_repository is None:
                    raise InvalidOperationError("reminder repository is required")
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
        now = utc_now().isoformat()
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

            for name in ("title", "type", "estimated_time", "status", "next_action"):
                if name not in supplied:
                    continue
                value = getattr(fields, name)
                if name in {"title", "type", "status"} and value is None:
                    continue
                updates[name] = value.value if isinstance(value, ItemStatus) else value

            for name, important_field in (
                ("importance", ImportantField.IMPORTANCE),
                ("urgency", ImportantField.URGENCY),
                ("deadline", ImportantField.DEADLINE),
            ):
                if name not in supplied or important_field not in evidence_fields:
                    continue
                value = getattr(fields, name)
                if name in {"importance", "urgency"}:
                    if value is None:
                        continue
                    # Unknown never erases an already known user/AI value.
                    if (
                        value == PriorityLevel.UNKNOWN
                        and item_row[name] != PriorityLevel.UNKNOWN.value
                    ):
                        continue
                    updates[name] = value.value
                else:
                    updates[name] = _serialize_deadline(value)

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
                    raise InvalidOperationError("reminder repository is required")
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

            for name, important_field in (
                ("importance", ImportantField.IMPORTANCE),
                ("urgency", ImportantField.URGENCY),
            ):
                value = getattr(fields, name)
                if (
                    name in supplied
                    and important_field in evidence_fields
                    and item_row[name] == PriorityLevel.UNKNOWN.value
                    and value is not None
                    and value != PriorityLevel.UNKNOWN
                ):
                    updates[name] = value.value

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

        important = {"importance", "urgency", "deadline"}
        if supplied & important and not patch.confirmed_important_fields:
            raise InvalidOperationError(
                "importance, urgency and deadline changes require explicit confirmation"
            )

        values: dict[str, Any] = {}
        for name in supplied:
            value = getattr(patch, name)
            if isinstance(value, (PriorityLevel, ItemStatus)):
                value = value.value
            elif name == "deadline":
                value = _serialize_deadline(value)
            elif name == "extra_information":
                value = _json_dump(value)
            values[name] = value
        values["updated_time"] = utc_now().isoformat()

        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            if exists is None:
                raise NotFoundError("item not found")
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

    def permanently_delete_item(self, item_id: int, user_id: int) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT status FROM personal_items
                WHERE id = ? AND user_id = ?
                """,
                (item_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("item not found")
            if row["status"] != ItemStatus.TRASH.value:
                raise InvalidOperationError(
                    "an item must be in trash before permanent deletion"
                )
            storage_keys = [
                str(segment["storage_key"])
                for segment in connection.execute(
                    """
                    SELECT voice_segments.storage_key
                    FROM voice_segments
                    JOIN item_inputs
                      ON item_inputs.id = voice_segments.item_input_id
                     AND item_inputs.user_id = voice_segments.user_id
                    WHERE item_inputs.item_id = ?
                      AND item_inputs.user_id = ?
                    """,
                    (item_id, user_id),
                )
            ]
            VoiceDeletionLedger.record(
                connection,
                storage_keys,
                "item_permanent_delete",
            )
            connection.execute(
                "DELETE FROM personal_items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            )
