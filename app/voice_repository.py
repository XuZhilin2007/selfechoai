from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.database import Database
from app.repository import NotFoundError
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_storage import StoredOriginal
from app.voice_contracts import (
    ALIBABA_ASR_MODEL,
    ALIBABA_ASR_PROVIDER,
    MAX_CAPTURE_TEXT_LENGTH,
    validate_client_segment_id,
)


class DraftRevisionConflictError(RuntimeError):
    pass


class DraftBlockedError(RuntimeError):
    pass


class DraftTextLimitError(RuntimeError):
    pass


class VoiceSegmentConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VoiceSegmentRecord:
    id: int
    user_id: int
    draft_id: int | None
    item_input_id: int | None
    position: int
    client_segment_id: str
    storage_key: str
    original_size_bytes: int
    original_sha256: str
    client_content_type: str | None
    detected_container: str | None
    detected_codec: str | None
    sample_rate_hz: int | None
    channels: int | None
    duration_ms: int | None
    transcription_status: str
    asr_input_kind: str | None
    provider: str
    model: str
    provider_transcript: str | None
    provider_request_id: str | None
    failure_code: str | None
    failure_message: str | None
    attempt_count: int
    created_time: datetime
    updated_time: datetime
    transcription_started_time: datetime | None
    transcription_finished_time: datetime | None


@dataclass(frozen=True, slots=True)
class CaptureDraftRecord:
    id: int
    user_id: int
    current_text: str
    revision: int
    created_time: datetime
    updated_time: datetime
    voice_segments: tuple[VoiceSegmentRecord, ...]


@dataclass(frozen=True, slots=True)
class VoiceSegmentUploadResult:
    segment: VoiceSegmentRecord
    created: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class VoiceCaptureRepository:
    """User-scoped Draft and Voice Segment persistence without HTTP concerns."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> VoiceSegmentRecord:
        return VoiceSegmentRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            draft_id=row["draft_id"],
            item_input_id=row["item_input_id"],
            position=int(row["position"]),
            client_segment_id=str(row["client_segment_id"]),
            storage_key=str(row["storage_key"]),
            original_size_bytes=int(row["original_size_bytes"]),
            original_sha256=str(row["original_sha256"]),
            client_content_type=row["client_content_type"],
            detected_container=row["detected_container"],
            detected_codec=row["detected_codec"],
            sample_rate_hz=row["sample_rate_hz"],
            channels=row["channels"],
            duration_ms=row["duration_ms"],
            transcription_status=str(row["transcription_status"]),
            asr_input_kind=row["asr_input_kind"],
            provider=str(row["provider"]),
            model=str(row["model"]),
            provider_transcript=row["provider_transcript"],
            provider_request_id=row["provider_request_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            attempt_count=int(row["attempt_count"]),
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
            transcription_started_time=_parse_optional_datetime(
                row["transcription_started_time"]
            ),
            transcription_finished_time=_parse_optional_datetime(
                row["transcription_finished_time"]
            ),
        )

    def _draft_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CaptureDraftRecord:
        segments = connection.execute(
            """
            SELECT * FROM voice_segments
            WHERE draft_id = ? AND user_id = ?
            ORDER BY position ASC, id ASC
            """,
            (row["id"], row["user_id"]),
        ).fetchall()
        return CaptureDraftRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            current_text=str(row["current_text"]),
            revision=int(row["revision"]),
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
            voice_segments=tuple(self._segment_from_row(segment) for segment in segments),
        )

    def get_draft(self, user_id: int) -> CaptureDraftRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM capture_drafts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return self._draft_from_row(connection, row)

    def put_draft(
        self,
        user_id: int,
        current_text: str,
        expected_revision: int,
    ) -> CaptureDraftRecord:
        if not isinstance(current_text, str):
            raise TypeError("current_text must be a string")
        if len(current_text) > MAX_CAPTURE_TEXT_LENGTH:
            raise DraftTextLimitError(
                f"Capture Draft exceeds {MAX_CAPTURE_TEXT_LENGTH:,} characters"
            )
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")

        now = utc_now_iso()
        with self.database.transaction() as connection:
            user_exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user_exists is None:
                raise NotFoundError("user not found")

            row = connection.execute(
                "SELECT * FROM capture_drafts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise DraftRevisionConflictError(
                        "Capture Draft revision is stale"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO capture_drafts (
                        user_id, current_text, revision, created_time, updated_time
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (user_id, current_text, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM capture_drafts WHERE id = ? AND user_id = ?",
                    (cursor.lastrowid, user_id),
                ).fetchone()
            else:
                if int(row["revision"]) != expected_revision:
                    raise DraftRevisionConflictError(
                        "Capture Draft revision is stale"
                    )
                active = connection.execute(
                    """
                    SELECT 1 FROM voice_segments
                    WHERE draft_id = ? AND user_id = ?
                      AND transcription_status IN ('pending', 'transcribing')
                    LIMIT 1
                    """,
                    (row["id"], user_id),
                ).fetchone()
                if active is not None:
                    raise DraftBlockedError(
                        "Capture Draft cannot be edited during Voice transcription"
                    )
                cursor = connection.execute(
                    """
                    UPDATE capture_drafts
                    SET current_text = ?, revision = revision + 1, updated_time = ?
                    WHERE id = ? AND user_id = ? AND revision = ?
                    """,
                    (current_text, now, row["id"], user_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise DraftRevisionConflictError(
                        "Capture Draft revision is stale"
                    )
                row = connection.execute(
                    "SELECT * FROM capture_drafts WHERE id = ? AND user_id = ?",
                    (row["id"], user_id),
                ).fetchone()
            return self._draft_from_row(connection, row)

    def delete_draft(self, user_id: int, expected_revision: int) -> bool:
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id, revision FROM capture_drafts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            if int(row["revision"]) != expected_revision:
                raise DraftRevisionConflictError("Capture Draft revision is stale")
            storage_keys = [
                str(segment["storage_key"])
                for segment in connection.execute(
                    """
                    SELECT storage_key FROM voice_segments
                    WHERE draft_id = ? AND user_id = ?
                    """,
                    (row["id"], user_id),
                )
            ]
            VoiceDeletionLedger.record(connection, storage_keys, "draft_discard")
            connection.execute(
                "DELETE FROM capture_drafts WHERE id = ? AND user_id = ?",
                (row["id"], user_id),
            )
        return True

    def create_pending_segment(
        self,
        *,
        user_id: int,
        client_segment_id: str,
        saved: StoredOriginal,
        client_content_type: str | None,
        expected_revision: int,
    ) -> VoiceSegmentUploadResult:
        validate_client_segment_id(client_segment_id)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        now = utc_now_iso()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM voice_segments
                WHERE user_id = ? AND client_segment_id = ?
                """,
                (user_id, client_segment_id),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["original_size_bytes"]) != saved.size_bytes
                    or existing["original_sha256"] != saved.sha256
                ):
                    raise VoiceSegmentConflictError(
                        "client_segment_id already refers to different audio"
                    )
                return VoiceSegmentUploadResult(
                    segment=self._segment_from_row(existing),
                    created=False,
                )

            if not saved.path.is_file():
                raise VoiceSegmentConflictError(
                    "Original Audio is no longer available"
                )

            draft = connection.execute(
                "SELECT * FROM capture_drafts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if draft is None:
                raise NotFoundError("Capture Draft not found")
            if int(draft["revision"]) != expected_revision:
                raise DraftRevisionConflictError("Capture Draft revision is stale")

            blocked = connection.execute(
                """
                SELECT transcription_status FROM voice_segments
                WHERE draft_id = ? AND user_id = ?
                  AND transcription_status IN ('pending', 'transcribing', 'failed')
                ORDER BY id ASC LIMIT 1
                """,
                (draft["id"], user_id),
            ).fetchone()
            if blocked is not None:
                if blocked["transcription_status"] == "failed":
                    raise DraftBlockedError(
                        "delete or retry the failed Voice Segment first"
                    )
                raise DraftBlockedError(
                    "another Voice Segment is still being transcribed"
                )

            next_position = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(position), -1) + 1
                    FROM voice_segments
                    WHERE draft_id = ? AND user_id = ?
                    """,
                    (draft["id"], user_id),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO voice_segments (
                    user_id, draft_id, item_input_id, position,
                    client_segment_id, storage_key, original_size_bytes,
                    original_sha256, client_content_type,
                    transcription_status, provider, model,
                    created_time, updated_time
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    draft["id"],
                    next_position,
                    client_segment_id,
                    saved.storage_key,
                    saved.size_bytes,
                    saved.sha256,
                    client_content_type,
                    ALIBABA_ASR_PROVIDER,
                    ALIBABA_ASR_MODEL,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM voice_file_deletions WHERE storage_key = ?",
                (saved.storage_key,),
            )
            row = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (cursor.lastrowid, user_id),
            ).fetchone()
            return VoiceSegmentUploadResult(
                segment=self._segment_from_row(row),
                created=True,
            )

    def get_segment(self, segment_id: int, user_id: int) -> VoiceSegmentRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("Voice Segment not found")
        return self._segment_from_row(row)

    def get_segment_record(self, segment_id: int, user_id: int) -> VoiceSegmentRecord:
        return self.get_segment(segment_id, user_id)

    def claim_pending_segment(self, segment_id: int, user_id: int) -> bool:
        now = utc_now_iso()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_segments
                SET transcription_status = 'transcribing',
                    attempt_count = attempt_count + 1,
                    updated_time = ?,
                    transcription_started_time = ?,
                    transcription_finished_time = NULL,
                    failure_code = NULL,
                    failure_message = NULL
                WHERE id = ? AND user_id = ?
                  AND transcription_status = 'pending'
                """,
                (now, now, segment_id, user_id),
            )
        return cursor.rowcount == 1

    def delete_failed_segment(self, segment_id: int, user_id: int) -> None:
        with self.database.transaction() as connection:
            segment = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            ).fetchone()
            if segment is None:
                raise NotFoundError("Voice Segment not found")
            if segment["draft_id"] is None:
                raise DraftBlockedError("saved Voice Segment cannot be deleted here")
            if segment["transcription_status"] != "failed":
                raise DraftBlockedError("only failed Voice Segments can be deleted")
            VoiceDeletionLedger.record(
                connection,
                [str(segment["storage_key"])],
                "segment_delete",
            )
            connection.execute(
                "DELETE FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            )
