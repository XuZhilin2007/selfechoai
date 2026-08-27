from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = Path("data/selfecho.db")
LEGACY_DATABASE_PATH = Path("data/personal_ai_inbox.db")


def default_database_path() -> Path:
    """Use the SelfEcho filename while preserving an existing pre-rename database."""

    if LEGACY_DATABASE_PATH.exists() and not DEFAULT_DATABASE_PATH.exists():
        return LEGACY_DATABASE_PATH
    return DEFAULT_DATABASE_PATH


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    registration_mode: str = "closed"
    invite_code_hash: str = ""
    app_origin: str = "http://127.0.0.1:8000"
    session_expiration_seconds: int = 2_592_000
    session_cookie_secure: bool = False
    ai_provider: str = "deepseek"
    ai_api_url: str = "https://api.openai.com/v1/responses"
    ai_api_key: str = ""
    ai_model: str = ""
    deepseek_api_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    ai_timeout_seconds: float = 30.0
    ai_debug_output: bool = False

    def __post_init__(self) -> None:
        if self.app_origin.lower().startswith("https://") and not self.session_cookie_secure:
            raise ValueError(
                "AUTH_COOKIE_SECURE must be true when APP_ORIGIN uses HTTPS"
            )

    @classmethod
    def from_environment(cls, env_file: Path | None = None) -> "Settings":
        """Load process variables first, then the project-root .env file.

        Values are read without mutating os.environ, which keeps app startup and
        tests deterministic. A real process environment always overrides .env.
        """

        file_values = dotenv_values(env_file or PROJECT_ROOT / ".env")

        def read(name: str, default: str = "") -> str:
            process_value = os.getenv(name)
            if process_value is not None:
                return process_value
            file_value = file_values.get(name)
            return str(file_value) if file_value is not None else default

        def read_bool(name: str, default: str) -> bool:
            value = read(name, default).strip().lower()
            if value not in {
                "true",
                "false",
                "1",
                "0",
                "yes",
                "no",
                "on",
                "off",
            }:
                raise ValueError(
                    f"{name} must be true/false, 1/0, yes/no, or on/off"
                )
            return value in {"true", "1", "yes", "on"}

        api_key = read("AI_API_KEY") or read("OPENAI_API_KEY")
        database_value = read("APP_DATABASE_PATH").strip()
        registration_mode = read("AUTH_REGISTRATION_MODE", "closed").strip().lower()
        if registration_mode not in {"closed", "invite"}:
            raise ValueError("AUTH_REGISTRATION_MODE must be closed or invite")
        app_origin = read("APP_ORIGIN", "http://127.0.0.1:8000").strip().rstrip("/")
        if not app_origin:
            raise ValueError("APP_ORIGIN must not be empty")
        session_expiration_seconds = int(
            read("AUTH_SESSION_EXPIRATION_SECONDS", "2592000")
        )
        if session_expiration_seconds <= 0:
            raise ValueError("AUTH_SESSION_EXPIRATION_SECONDS must be positive")
        session_cookie_secure = read_bool("AUTH_COOKIE_SECURE", "false")
        return cls(
            database_path=(
                Path(database_value) if database_value else default_database_path()
            ),
            registration_mode=registration_mode,
            invite_code_hash=read("AUTH_INVITE_CODE_HASH").strip().lower(),
            app_origin=app_origin,
            session_expiration_seconds=session_expiration_seconds,
            session_cookie_secure=session_cookie_secure,
            ai_provider=read("AI_PROVIDER", "deepseek").strip().lower(),
            ai_api_url=read(
                "AI_API_URL", "https://api.openai.com/v1/responses"
            ).strip(),
            ai_api_key=api_key.strip(),
            ai_model=read("AI_MODEL").strip(),
            deepseek_api_url=read(
                "DEEPSEEK_API_URL", "https://api.deepseek.com"
            ).strip(),
            deepseek_api_key=read("DEEPSEEK_API_KEY").strip(),
            deepseek_model=read(
                "DEEPSEEK_MODEL", "deepseek-v4-flash"
            ).strip(),
            ai_timeout_seconds=float(read("AI_TIMEOUT_SECONDS", "30")),
            ai_debug_output=read_bool("AI_DEBUG_OUTPUT", "false"),
        )
