from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from app.auth_repository import AuthRepository, DuplicateUserError, utc_now
from app.schemas import UserPublic, UserRecord, UserSessionRecord, UserStatus


_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_TOKEN_BYTES = 32
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(_TOKEN_BYTES))


class AuthenticationError(Exception):
    pass


class RegistrationClosedError(AuthenticationError):
    pass


class InvalidInviteCodeError(AuthenticationError):
    pass


class DuplicateEmailError(AuthenticationError):
    pass


class InvalidCredentialsError(AuthenticationError):
    pass


class InvalidSessionError(AuthenticationError):
    pass


class InvalidCsrfTokenError(AuthenticationError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    user: UserPublic
    session_token: str
    csrf_token: str
    expires_time: datetime


@dataclass(frozen=True, slots=True)
class ValidatedSession:
    user: UserRecord
    session: UserSessionRecord

    @property
    def public_user(self) -> UserPublic:
        return _public_user(self.user)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    return _hash_token(token, name="session token")


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_csrf_token(token: str) -> str:
    return _hash_token(token, name="CSRF token")


def hash_invite_code(invite_code: str) -> str:
    return _hash_token(invite_code, name="invite code")


def _hash_token(token: str, *, name: str) -> str:
    if not token:
        raise ValueError(f"{name} must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _public_user(user: UserRecord) -> UserPublic:
    return UserPublic(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        timezone=user.timezone,
        created_time=user.created_time,
        updated_time=user.updated_time,
    )


class AuthenticationService:
    def __init__(
        self,
        repository: AuthRepository,
        *,
        registration_mode: str,
        invite_code_hash: str,
        session_expiration_seconds: int,
    ) -> None:
        if registration_mode not in {"closed", "invite"}:
            raise ValueError("registration_mode must be closed or invite")
        if session_expiration_seconds <= 0:
            raise ValueError("session_expiration_seconds must be positive")
        normalized_invite_hash = invite_code_hash.strip().lower()
        if registration_mode == "invite" and (
            len(normalized_invite_hash) != 64
            or any(character not in "0123456789abcdef" for character in normalized_invite_hash)
        ):
            raise ValueError(
                "invite registration requires a SHA-256 invite code hash"
            )
        self.repository = repository
        self.registration_mode = registration_mode
        self.invite_code_hash = normalized_invite_hash
        self.session_expiration_seconds = session_expiration_seconds

    def register(
        self,
        *,
        invite_code: str,
        email: str,
        password: str,
        display_name: str,
        timezone_name: str,
        user_agent: str | None = None,
    ) -> AuthenticationResult:
        self._validate_invite_code(invite_code)
        normalized_email = normalize_email(email)
        if self.repository.get_user_by_email(normalized_email) is not None:
            raise DuplicateEmailError("email is already registered")
        try:
            user = self.repository.create_user(
                email=normalized_email,
                password_hash=hash_password(password),
                display_name=display_name.strip(),
                timezone_name=timezone_name.strip(),
            )
        except DuplicateUserError as exc:
            raise DuplicateEmailError("email is already registered") from exc
        return self._create_session(user, user_agent=user_agent)

    def login(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None = None,
    ) -> AuthenticationResult:
        user = self.repository.get_user_by_email(normalize_email(email))
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_matches = verify_password(password, password_hash)
        if (
            user is None
            or not password_matches
            or user.status != UserStatus.ACTIVE
        ):
            raise InvalidCredentialsError("invalid email or password")
        return self._create_session(user, user_agent=user_agent)

    def logout(self, session_token: str, csrf_token: str | None) -> None:
        validated = self.validate_session(session_token, touch=False)
        self.validate_csrf_token(validated, csrf_token)
        if not self.repository.revoke_session(validated.session.id):
            raise InvalidSessionError("session is not valid")

    def resolve_current_user(self, session_token: str) -> UserPublic:
        validated = self.validate_session(session_token)
        return validated.public_user

    def validate_session(
        self, session_token: str, *, touch: bool = True
    ) -> ValidatedSession:
        if not session_token:
            raise InvalidSessionError("session is not valid")
        session = self.repository.get_session_by_token_hash(
            hash_session_token(session_token)
        )
        now = utc_now()
        if (
            session is None
            or session.revoked_time is not None
            or _as_utc(session.expires_time) <= now
        ):
            raise InvalidSessionError("session is not valid")
        user = self.repository.get_user_by_id(session.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise InvalidSessionError("session is not valid")
        if touch and not self.repository.touch_session(session.id, now):
            raise InvalidSessionError("session is not valid")
        return ValidatedSession(user=user, session=session)

    def validate_csrf_token(
        self, validated: ValidatedSession, csrf_token: str | None
    ) -> None:
        if not csrf_token:
            raise InvalidCsrfTokenError("CSRF token is not valid")
        candidate_hash = hash_csrf_token(csrf_token)
        if not secrets.compare_digest(
            candidate_hash, validated.session.csrf_token_hash
        ):
            raise InvalidCsrfTokenError("CSRF token is not valid")

    def _validate_invite_code(self, invite_code: str) -> None:
        if self.registration_mode != "invite":
            raise RegistrationClosedError("registration is closed")
        if not invite_code or not secrets.compare_digest(
            hash_invite_code(invite_code), self.invite_code_hash
        ):
            raise InvalidInviteCodeError("invite code is not valid")

    def _create_session(
        self, user: UserRecord, *, user_agent: str | None
    ) -> AuthenticationResult:
        session_token = generate_session_token()
        csrf_token = generate_csrf_token()
        expires_time = utc_now() + timedelta(
            seconds=self.session_expiration_seconds
        )
        self.repository.create_session(
            user_id=user.id,
            token_hash=hash_session_token(session_token),
            csrf_token_hash=hash_csrf_token(csrf_token),
            expires_time=expires_time,
            user_agent=user_agent[:500] if user_agent else None,
        )
        return AuthenticationResult(
            user=_public_user(user),
            session_token=session_token,
            csrf_token=csrf_token,
            expires_time=expires_time,
        )
