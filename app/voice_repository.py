from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.database import Database
from app.repository import NotFoundError, Repository
from app.schemas import ItemInputPublic
from app.services.voice_deletions import VoiceDeletionLedger
from app.services.voice_media import MediaMetadata
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


@dataclass(frozen=True, slots=True)
class PendingVoiceSegment:
    segment_id: int
    user_id: int


@dataclass(frozen=True, slots=True)
class FinalSaveResult:
    item_input: ItemInputPublic
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

    def save_draft(
        self,
        user_id: int,
        draft_id: int,
        expected_revision: int,
    ) -> FinalSaveResult:
        if draft_id <= 0:
            raise ValueError("draft_id must be positive")
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        now = utc_now_iso()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM item_inputs
                WHERE user_id = ? AND source_draft_id = ?
                """,
                (user_id, draft_id),
            ).fetchone()
            if existing is not None:
                segment_ids = Repository._voice_segment_ids(
                    connection,
                    int(existing["id"]),
                    user_id,
                )
                return FinalSaveResult(
                    item_input=Repository._input_from_row(existing, segment_ids),
                    created=False,
                )

            draft = connection.execute(
                """
                SELECT * FROM capture_drafts
                WHERE id = ? AND user_id = ?
                """,
                (draft_id, user_id),
            ).fetchone()
            if draft is None:
                raise NotFoundError("Capture Draft not found")
            if int(draft["revision"]) != expected_revision:
                raise DraftRevisionConflictError("Capture Draft revision is stale")
            current_text = str(draft["current_text"])
            if not current_text.strip():
                raise DraftBlockedError("Capture Draft text must not be blank")

            segments = connection.execute(
                """
                SELECT * FROM voice_segments
                WHERE draft_id = ? AND user_id = ?
                ORDER BY position ASC, id ASC
                """,
                (draft_id, user_id),
            ).fetchall()
            incomplete = [
                str(segment["transcription_status"])
                for segment in segments
                if segment["transcription_status"] != "succeeded"
            ]
            if incomplete:
                if "failed" in incomplete:
                    raise DraftBlockedError(
                        "failed Voice Segments must be retried or deleted before Save"
                    )
                raise DraftBlockedError(
                    "Voice transcription must finish before Save"
                )

            input_method = "voice" if segments else "text"
            cursor = connection.execute(
                """
                INSERT INTO item_inputs (
                    user_id, item_id, source_draft_id, original_text,
                    input_method, processing_status, created_time
                ) VALUES (?, NULL, ?, ?, ?, 'pending', ?)
                """,
                (user_id, draft_id, current_text, input_method, now),
            )
            input_id = int(cursor.lastrowid)
            updated = connection.execute(
                """
                UPDATE voice_segments
                SET draft_id = NULL, item_input_id = ?, updated_time = ?
                WHERE draft_id = ? AND user_id = ?
                """,
                (input_id, now, draft_id, user_id),
            )
            if updated.rowcount != len(segments):
                raise RuntimeError("Voice Segment reparent count changed during Save")
            deleted = connection.execute(
                "DELETE FROM capture_drafts WHERE id = ? AND user_id = ?",
                (draft_id, user_id),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Capture Draft changed during Save")
            row = connection.execute(
                "SELECT * FROM item_inputs WHERE id = ? AND user_id = ?",
                (input_id, user_id),
            ).fetchone()
            return FinalSaveResult(
                item_input=Repository._input_from_row(
                    row,
                    [int(segment["id"]) for segment in segments],
                ),
                created=True,
            )

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

    def set_media_metadata(
        self,
        segment_id: int,
        user_id: int,
        metadata: MediaMetadata,
    ) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_segments
                SET detected_container = ?, detected_codec = ?,
                    sample_rate_hz = ?, channels = ?, duration_ms = ?,
                    updated_time = ?
                WHERE id = ? AND user_id = ?
                  AND transcription_status = 'transcribing'
                """,
                (
                    metadata.container,
                    metadata.codec,
                    metadata.sample_rate_hz,
                    metadata.channels,
                    metadata.duration_ms,
                    utc_now_iso(),
                    segment_id,
                    user_id,
                ),
            )
        return cursor.rowcount == 1

    def set_asr_input_kind(
        self,
        segment_id: int,
        user_id: int,
        input_kind: str,
    ) -> bool:
        if input_kind not in {"original_direct", "derived_wav"}:
            raise ValueError("invalid ASR input kind")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_segments
                SET asr_input_kind = ?, updated_time = ?
                WHERE id = ? AND user_id = ?
                  AND transcription_status = 'transcribing'
                """,
                (input_kind, utc_now_iso(), segment_id, user_id),
            )
        return cursor.rowcount == 1

    def complete_transcription(
        self,
        segment_id: int,
        user_id: int,
        *,
        transcript: str,
        provider_request_id: str,
    ) -> VoiceSegmentRecord | None:
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("transcript must not be blank")
        if not isinstance(provider_request_id, str) or not provider_request_id.strip():
            raise ValueError("provider_request_id must not be blank")
        now = utc_now_iso()
        with self.database.transaction() as connection:
            segment = connection.execute(
                """
                SELECT * FROM voice_segments
                WHERE id = ? AND user_id = ?
                  AND transcription_status = 'transcribing'
                """,
                (segment_id, user_id),
            ).fetchone()
            if segment is None or segment["draft_id"] is None:
                return None
            draft = connection.execute(
                "SELECT * FROM capture_drafts WHERE id = ? AND user_id = ?",
                (segment["draft_id"], user_id),
            ).fetchone()
            if draft is None:
                return None

            appended = _append_transcript(str(draft["current_text"]), transcript)
            if len(appended) > MAX_CAPTURE_TEXT_LENGTH:
                connection.execute(
                    """
                    UPDATE voice_segments
                    SET transcription_status = 'failed',
                        provider_transcript = ?, provider_request_id = ?,
                        failure_code = 'draft_text_limit',
                        failure_message = ?, updated_time = ?,
                        transcription_finished_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        transcript,
                        provider_request_id,
                        "转写已完成，但加入 Draft 后会超过 10,000 字；请缩短文字后重试。",
                        now,
                        now,
                        segment_id,
                        user_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE capture_drafts
                    SET current_text = ?, revision = revision + 1, updated_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (appended, now, draft["id"], user_id),
                )
                connection.execute(
                    """
                    UPDATE voice_segments
                    SET transcription_status = 'succeeded',
                        provider_transcript = ?, provider_request_id = ?,
                        failure_code = NULL, failure_message = NULL,
                        updated_time = ?, transcription_finished_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        transcript,
                        provider_request_id,
                        now,
                        now,
                        segment_id,
                        user_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            ).fetchone()
            return self._segment_from_row(row)

    def mark_segment_failed(
        self,
        segment_id: int,
        user_id: int,
        *,
        failure_code: str,
        failure_message: str,
    ) -> bool:
        now = utc_now_iso()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_segments
                SET transcription_status = 'failed',
                    failure_code = ?, failure_message = ?,
                    updated_time = ?, transcription_finished_time = ?
                WHERE id = ? AND user_id = ?
                  AND transcription_status = 'transcribing'
                """,
                (
                    failure_code,
                    failure_message[:500],
                    now,
                    now,
                    segment_id,
                    user_id,
                ),
            )
        return cursor.rowcount == 1

    def retry_failed_segment(
        self,
        segment_id: int,
        user_id: int,
    ) -> tuple[VoiceSegmentRecord, bool]:
        now = utc_now_iso()
        with self.database.transaction() as connection:
            segment = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            ).fetchone()
            if segment is None:
                raise NotFoundError("Voice Segment not found")
            if segment["draft_id"] is None:
                raise DraftBlockedError("saved Voice Segment cannot be retried here")
            if segment["transcription_status"] != "failed":
                raise DraftBlockedError("only failed Voice Segments can be retried")
            draft = connection.execute(
                "SELECT * FROM capture_drafts WHERE id = ? AND user_id = ?",
                (segment["draft_id"], user_id),
            ).fetchone()
            if draft is None:
                raise NotFoundError("Capture Draft not found")

            if (
                segment["failure_code"] == "draft_text_limit"
                and segment["provider_transcript"] is not None
            ):
                appended = _append_transcript(
                    str(draft["current_text"]),
                    str(segment["provider_transcript"]),
                )
                if len(appended) > MAX_CAPTURE_TEXT_LENGTH:
                    raise DraftTextLimitError(
                        "Capture Draft is still too long for the completed transcript"
                    )
                connection.execute(
                    """
                    UPDATE capture_drafts
                    SET current_text = ?, revision = revision + 1, updated_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (appended, now, draft["id"], user_id),
                )
                connection.execute(
                    """
                    UPDATE voice_segments
                    SET transcription_status = 'succeeded',
                        failure_code = NULL, failure_message = NULL,
                        updated_time = ?, transcription_finished_time = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (now, now, segment_id, user_id),
                )
                row = connection.execute(
                    "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                    (segment_id, user_id),
                ).fetchone()
                return self._segment_from_row(row), False

            active = connection.execute(
                """
                SELECT 1 FROM voice_segments
                WHERE draft_id = ? AND user_id = ? AND id != ?
                  AND transcription_status IN ('pending', 'transcribing')
                """,
                (draft["id"], user_id, segment_id),
            ).fetchone()
            if active is not None:
                raise DraftBlockedError(
                    "another Voice Segment is still being transcribed"
                )
            connection.execute(
                """
                UPDATE voice_segments
                SET transcription_status = 'pending',
                    asr_input_kind = NULL,
                    provider_transcript = NULL,
                    provider_request_id = NULL,
                    failure_code = NULL,
                    failure_message = NULL,
                    updated_time = ?,
                    transcription_started_time = NULL,
                    transcription_finished_time = NULL
                WHERE id = ? AND user_id = ?
                """,
                (now, segment_id, user_id),
            )
            row = connection.execute(
                "SELECT * FROM voice_segments WHERE id = ? AND user_id = ?",
                (segment_id, user_id),
            ).fetchone()
            return self._segment_from_row(row), True

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

    def system_recover_interrupted_segments(self) -> int:
        now = utc_now_iso()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_segments
                SET transcription_status = 'failed',
                    failure_code = 'interrupted',
                    failure_message = '转写被服务重启中断；原始录音已保留，请手动重试。',
                    updated_time = ?, transcription_finished_time = ?
                WHERE transcription_status = 'transcribing'
                """,
                (now, now),
            )
        return cursor.rowcount

    def system_list_pending_segments(self) -> list[PendingVoiceSegment]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id FROM voice_segments
                WHERE transcription_status = 'pending' AND draft_id IS NOT NULL
                ORDER BY created_time ASC, id ASC
                """
            ).fetchall()
        return [
            PendingVoiceSegment(
                segment_id=int(row["id"]),
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]


def _append_transcript(current_text: str, transcript: str) -> str:
    if not current_text:
        return transcript
    if current_text[-1].isspace() or transcript[:1].isspace():
        return current_text + transcript
    return current_text + "\n" + transcript
