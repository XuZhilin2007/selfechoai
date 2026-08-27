from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.auth import (
    generate_csrf_token,
    generate_session_token,
    hash_csrf_token,
    hash_password,
    hash_session_token,
    normalize_email,
    verify_password,
)
from app.auth_repository import AuthRepository, utc_now
from app.database import Database
from app.schemas import UserPublic, UserStatus


def create_auth_repository(tmp_path: Path) -> AuthRepository:
    database = Database(tmp_path / "auth-foundation.db")
    database.initialize()
    return AuthRepository(database)


def test_email_normalization():
    assert normalize_email("  Test.User+Demo@EXAMPLE.COM  ") == (
        "test.user+demo@example.com"
    )
    assert normalize_email("ÜSER@Example.com") == "üser@example.com"


def test_argon2id_password_hashing_and_verification():
    password = "correct horse battery staple"

    first_hash = hash_password(password)
    second_hash = hash_password(password)
    different_password_hash = hash_password("a different strong password")

    assert first_hash.startswith("$argon2id$")
    assert second_hash.startswith("$argon2id$")
    assert first_hash != second_hash
    assert first_hash != different_password_hash
    assert verify_password(password, first_hash) is True
    assert verify_password("different password", first_hash) is False
    assert verify_password(password, "not-a-valid-hash") is False


def test_secure_session_and_csrf_tokens_are_random_and_hashable():
    first_session_token = generate_session_token()
    second_session_token = generate_session_token()
    csrf_token = generate_csrf_token()

    assert first_session_token != second_session_token
    assert csrf_token not in {first_session_token, second_session_token}
    assert hash_session_token(first_session_token) == hash_session_token(
        first_session_token
    )
    assert hash_session_token(first_session_token) != hash_session_token(
        second_session_token
    )
    assert len(hash_session_token(first_session_token)) == 64
    assert len(hash_csrf_token(csrf_token)) == 64
    assert first_session_token not in hash_session_token(first_session_token)


def test_user_persistence_and_public_model_do_not_expose_password_hash(
    tmp_path: Path,
):
    repository = create_auth_repository(tmp_path)
    original_password_hash = hash_password("owner password for tests")
    user = repository.create_user(
        email=normalize_email("Owner@Example.com"),
        password_hash=original_password_hash,
        display_name="Owner",
        timezone_name="Asia/Shanghai",
    )

    assert user.status == UserStatus.ACTIVE
    assert repository.get_user_by_email("owner@example.com") == user
    assert repository.get_user_by_id(user.id) == user

    updated = repository.update_user_profile(
        user.id,
        display_name="Updated Owner",
        timezone_name="Asia/Tokyo",
    )
    assert updated.display_name == "Updated Owner"
    assert updated.timezone == "Asia/Tokyo"

    new_password_hash = hash_password("new owner password for tests")
    password_updated = repository.update_password(user.id, new_password_hash)
    assert password_updated.password_hash == new_password_hash
    assert verify_password(
        "new owner password for tests", password_updated.password_hash
    )

    public_user = UserPublic(
        id=password_updated.id,
        email=password_updated.email,
        display_name=password_updated.display_name,
        timezone=password_updated.timezone,
        created_time=password_updated.created_time,
        updated_time=password_updated.updated_time,
    )
    assert "password_hash" not in public_user.model_dump()
    assert "status" not in public_user.model_dump()


def test_session_persistence_lookup_touch_revoke_and_cleanup(tmp_path: Path):
    repository = create_auth_repository(tmp_path)
    user = repository.create_user(
        email="session@example.com",
        password_hash=hash_password("session password for tests"),
        display_name="Session User",
        timezone_name="Asia/Shanghai",
    )
    now = utc_now()
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    session = repository.create_session(
        user_id=user.id,
        token_hash=token_hash,
        csrf_token_hash=hash_csrf_token(generate_csrf_token()),
        expires_time=now + timedelta(days=30),
        user_agent="SelfEcho test client",
    )

    found = repository.get_session_by_token_hash(token_hash)
    assert found == session
    assert found.revoked_time is None
    assert found.user_agent == "SelfEcho test client"

    touched_at = now + timedelta(minutes=10)
    assert repository.touch_session(session.id, touched_at) is True
    touched = repository.get_session_by_token_hash(token_hash)
    assert touched is not None
    assert touched.last_seen_time == touched_at

    revoked_at = now + timedelta(minutes=20)
    assert repository.revoke_session(session.id, revoked_at) is True
    assert repository.revoke_session(session.id, revoked_at) is False
    revoked = repository.get_session_by_token_hash(token_hash)
    assert revoked is not None
    assert revoked.revoked_time == revoked_at
    assert repository.touch_session(session.id, now + timedelta(minutes=30)) is False

    expired = repository.create_session(
        user_id=user.id,
        token_hash=hash_session_token(generate_session_token()),
        csrf_token_hash=hash_csrf_token(generate_csrf_token()),
        expires_time=now - timedelta(minutes=1),
    )
    assert repository.cleanup_expired_sessions(now) == 1
    assert repository.get_session_by_token_hash(expired.token_hash) is None

    active = repository.create_session(
        user_id=user.id,
        token_hash=hash_session_token(generate_session_token()),
        csrf_token_hash=hash_csrf_token(generate_csrf_token()),
        expires_time=now + timedelta(days=10),
    )
    assert repository.revoke_all_user_sessions(user.id) == 1
    revoked_active = repository.get_session_by_token_hash(active.token_hash)
    assert revoked_active is not None
    assert revoked_active.revoked_time is not None
