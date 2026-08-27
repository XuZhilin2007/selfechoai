from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.database import Database
from app.schemas import UserRecord, UserSessionRecord


class DuplicateUserError(Exception):
    pass


class FirstUserAlreadyExistsError(Exception):
    pass


class AuthRecordNotFoundError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(timezone.utc).isoformat()


class AuthRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"],
            display_name=row["display_name"],
            timezone=row["timezone"],
            status=row["status"],
            password_changed_time=datetime.fromisoformat(
                row["password_changed_time"]
            ),
            created_time=datetime.fromisoformat(row["created_time"]),
            updated_time=datetime.fromisoformat(row["updated_time"]),
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> UserSessionRecord:
        return UserSessionRecord(
            id=row["id"],
            user_id=row["user_id"],
            token_hash=row["token_hash"],
            csrf_token_hash=row["csrf_token_hash"],
            created_time=datetime.fromisoformat(row["created_time"]),
            last_seen_time=datetime.fromisoformat(row["last_seen_time"]),
            expires_time=datetime.fromisoformat(row["expires_time"]),
            revoked_time=(
                datetime.fromisoformat(row["revoked_time"])
                if row["revoked_time"] is not None
                else None
            ),
            user_agent=row["user_agent"],
        )

    def _insert_user(
        self,
        connection: sqlite3.Connection,
        *,
        email: str,
        password_hash: str,
        display_name: str,
        timezone_name: str,
    ) -> UserRecord:
        now = utc_now().isoformat()
        cursor = connection.execute(
            """
            INSERT INTO users (
                email, password_hash, display_name, timezone, status,
                password_changed_time, created_time, updated_time
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                email,
                password_hash,
                display_name,
                timezone_name,
                now,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._user_from_row(row)

    def create_user(
        self,
        *,
        email: str,
        password_hash: str,
        display_name: str,
        timezone_name: str,
    ) -> UserRecord:
        try:
            with self.database.transaction() as connection:
                return self._insert_user(
                    connection,
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    timezone_name=timezone_name,
                )
        except sqlite3.IntegrityError as exc:
            if "users.email" in str(exc):
                raise DuplicateUserError("email is already registered") from exc
            raise

    def count_users(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_first_user(
        self,
        *,
        email: str,
        password_hash: str,
        display_name: str,
        timezone_name: str,
    ) -> UserRecord:
        """Atomically create the only allowed bootstrap user for an empty database."""

        try:
            with self.database.transaction() as connection:
                user_count = int(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                )
                if user_count != 0:
                    raise FirstUserAlreadyExistsError(
                        "bootstrap is only allowed when the database has no users"
                    )
                return self._insert_user(
                    connection,
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    timezone_name=timezone_name,
                )
        except sqlite3.IntegrityError as exc:
            if "users.email" in str(exc):
                raise DuplicateUserError("email is already registered") from exc
            raise

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def update_user_profile(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        timezone_name: str | None = None,
    ) -> UserRecord:
        updates: dict[str, str] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if timezone_name is not None:
            updates["timezone"] = timezone_name
        if not updates:
            raise ValueError("at least one profile field is required")
        updates["updated_time"] = utc_now().isoformat()

        with self.database.transaction() as connection:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            cursor = connection.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                [*updates.values(), user_id],
            )
            if cursor.rowcount != 1:
                raise AuthRecordNotFoundError("user not found")
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user_from_row(row)

    def update_password(self, user_id: int, password_hash: str) -> UserRecord:
        now = utc_now().isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET password_hash = ?, password_changed_time = ?, updated_time = ?
                WHERE id = ?
                """,
                (password_hash, now, now, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthRecordNotFoundError("user not found")
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user_from_row(row)

    def create_session(
        self,
        *,
        user_id: int,
        token_hash: str,
        csrf_token_hash: str,
        expires_time: datetime,
        user_agent: str | None = None,
    ) -> UserSessionRecord:
        now = utc_now().isoformat()
        expires = _serialize_datetime(expires_time)
        with self.database.transaction() as connection:
            user_exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user_exists is None:
                raise AuthRecordNotFoundError("user not found")
            cursor = connection.execute(
                """
                INSERT INTO user_sessions (
                    user_id, token_hash, csrf_token_hash, created_time,
                    last_seen_time, expires_time, revoked_time, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    user_id,
                    token_hash,
                    csrf_token_hash,
                    now,
                    now,
                    expires,
                    user_agent,
                ),
            )
            row = connection.execute(
                "SELECT * FROM user_sessions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._session_from_row(row)

    def get_session_by_token_hash(
        self, token_hash: str
    ) -> UserSessionRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def touch_session(
        self, session_id: int, seen_time: datetime | None = None
    ) -> bool:
        seen = _serialize_datetime(seen_time or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions SET last_seen_time = ?
                WHERE id = ? AND revoked_time IS NULL
                """,
                (seen, session_id),
            )
        return cursor.rowcount == 1

    def revoke_session(
        self, session_id: int, revoked_time: datetime | None = None
    ) -> bool:
        revoked = _serialize_datetime(revoked_time or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions SET revoked_time = ?
                WHERE id = ? AND revoked_time IS NULL
                """,
                (revoked, session_id),
            )
        return cursor.rowcount == 1

    def revoke_all_user_sessions(
        self, user_id: int, revoked_time: datetime | None = None
    ) -> int:
        revoked = _serialize_datetime(revoked_time or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions SET revoked_time = ?
                WHERE user_id = ? AND revoked_time IS NULL
                """,
                (revoked, user_id),
            )
        return cursor.rowcount

    def cleanup_expired_sessions(self, cutoff: datetime | None = None) -> int:
        cutoff_value = _serialize_datetime(cutoff or utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM user_sessions WHERE expires_time <= ?",
                (cutoff_value,),
            )
        return cursor.rowcount
