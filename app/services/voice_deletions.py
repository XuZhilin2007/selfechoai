from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.database import Database
from app.services.voice_storage import VoiceStorage


VOICE_DELETION_REASONS = frozenset(
    {
        "segment_delete",
        "draft_discard",
        "failed_input_delete",
        "item_permanent_delete",
        "orphan_cleanup",
    }
)
ORPHAN_CLEANUP_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class VoiceDeletionDrainResult:
    selected: int
    deleted_or_absent: int
    failed: int


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VoiceDeletionLedger:
    """A bounded ledger for DB-first, filesystem-second Voice deletion."""

    def __init__(self, database: Database, storage: VoiceStorage) -> None:
        self.database = database
        self.storage = storage

    @staticmethod
    def record(
        connection: sqlite3.Connection,
        storage_keys: Iterable[str],
        reason: str,
        *,
        created_time: str | None = None,
    ) -> None:
        if reason not in VOICE_DELETION_REASONS:
            raise ValueError("unsupported Voice deletion reason")
        timestamp = created_time or utc_now_iso()
        for storage_key in dict.fromkeys(storage_keys):
            connection.execute(
                """
                INSERT OR IGNORE INTO voice_file_deletions (
                    storage_key, reason, created_time
                ) VALUES (?, ?, ?)
                """,
                (storage_key, reason, timestamp),
            )

    def drain(self, *, limit: int = 25) -> VoiceDeletionDrainResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        orphan_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=ORPHAN_CLEANUP_GRACE_SECONDS
        )
        cutoff_text = orphan_cutoff.isoformat()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT deletion.id
                FROM voice_file_deletions AS deletion
                WHERE NOT EXISTS (
                    SELECT 1 FROM voice_segments AS segment
                    WHERE segment.storage_key = deletion.storage_key
                )
                  AND (
                    deletion.reason != 'orphan_cleanup'
                    OR deletion.created_time <= ?
                  )
                ORDER BY deletion.created_time ASC, deletion.id ASC
                LIMIT ?
                """,
                (cutoff_text, limit),
            ).fetchall()

        completed = 0
        failed = 0
        for row in rows:
            outcome = self._drain_one(int(row["id"]), cutoff_text)
            if outcome == "failed":
                failed += 1
            elif outcome == "completed":
                completed += 1

        self.storage.cleanup_stale_parts(older_than=orphan_cutoff, limit=limit)
        self.storage.cleanup_stale_tmp_files(older_than=orphan_cutoff, limit=limit)
        return VoiceDeletionDrainResult(
            selected=len(rows),
            deleted_or_absent=completed,
            failed=failed,
        )

    def _drain_one(self, deletion_id: int, cutoff_text: str) -> str:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT deletion.storage_key
                FROM voice_file_deletions AS deletion
                WHERE deletion.id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM voice_segments AS segment
                    WHERE segment.storage_key = deletion.storage_key
                  )
                  AND (
                    deletion.reason != 'orphan_cleanup'
                    OR deletion.created_time <= ?
                  )
                """,
                (deletion_id, cutoff_text),
            ).fetchone()
            if row is None:
                return "skipped"
            try:
                self.storage.delete_original(str(row["storage_key"]))
            except Exception as exc:
                diagnostic = f"{type(exc).__name__}: {exc}"[:500]
                connection.execute(
                    """
                    UPDATE voice_file_deletions
                    SET attempt_count = attempt_count + 1,
                        last_attempt_time = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (utc_now_iso(), diagnostic, deletion_id),
                )
                return "failed"
            connection.execute(
                "DELETE FROM voice_file_deletions WHERE id = ?",
                (deletion_id,),
            )
            return "completed"
