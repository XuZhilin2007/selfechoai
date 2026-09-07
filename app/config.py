from __future__ import annotations

import base64
import binascii
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from dotenv import dotenv_values
from pydantic import SecretStr
from py_vapid import Vapid02

from app.voice_contracts import DEFAULT_VOICE_MAX_UPLOAD_BYTES


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = Path("data/selfecho.db")
LEGACY_DATABASE_PATH = Path("data/personal_ai_inbox.db")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
MAX_VOICE_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_VOICE_ASR_TIMEOUT_SECONDS = 120.0


def _decode_base64url(value: str, *, field_name: str) -> bytes:
    if not value or not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be valid base64url")
    if "=" in value[:-2]:
        raise ValueError(f"{field_name} must be valid base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{field_name} must be valid base64url") from exc


def _validate_vapid_subject(value: str) -> None:
    if (
        value != value.strip()
        or not value
        or len(value) > 2_048
        or any(ord(character) < 33 for character in value)
    ):
        raise ValueError(
            "WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(
            "WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI"
        ) from exc
    if parsed.scheme == "mailto":
        if (
            not parsed.path
            or parsed.path.count("@") != 1
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI"
            )
        return
    if parsed.scheme == "https":
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI"
            ) from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise ValueError(
                "WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI"
            )
        return
    raise ValueError("WEB_PUSH_VAPID_SUBJECT must be a valid mailto or HTTPS URI")


def _validate_vapid_key_pair(public_value: str, private_value: str) -> None:
    if len(public_value) > 256:
        raise ValueError("WEB_PUSH_VAPID_PUBLIC_KEY must be an uncompressed P-256 key")
    if len(private_value) > 4_096:
        raise ValueError("WEB_PUSH_VAPID_PRIVATE_KEY must be a valid P-256 key")
    public_bytes = _decode_base64url(
        public_value,
        field_name="WEB_PUSH_VAPID_PUBLIC_KEY",
    )
    if len(public_bytes) != 65 or public_bytes[0] != 4:
        raise ValueError("WEB_PUSH_VAPID_PUBLIC_KEY must be an uncompressed P-256 key")
    try:
        vapid = Vapid02.from_string(private_value)
        derived_public = vapid.public_key.public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "WEB_PUSH_VAPID_PRIVATE_KEY must be a valid P-256 key"
        ) from exc
    if derived_public != public_bytes:
        raise ValueError(
            "WEB_PUSH_VAPID_PUBLIC_KEY and WEB_PUSH_VAPID_PRIVATE_KEY must match"
        )


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
    web_push_enabled: bool = False
    web_push_vapid_public_key: str = ""
    web_push_vapid_private_key: SecretStr = SecretStr("")
    web_push_vapid_subject: str = ""
    web_push_test_send_enabled: bool = False
    web_push_timeout_seconds: float = 10.0
    reminder_worker_enabled: bool = False
    reminder_poll_interval_seconds: float = 30.0
    reminder_batch_size: int = 100
    reminder_sending_stale_seconds: int = 300
    voice_asr_enabled: bool = False
    voice_storage_root: Path | None = None
    voice_max_upload_bytes: int = DEFAULT_VOICE_MAX_UPLOAD_BYTES
    ffprobe_path: Path | None = None
    ffmpeg_path: Path | None = None
    alibaba_asr_api_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    alibaba_api_key: SecretStr = SecretStr("")
    voice_asr_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            self.app_origin.lower().startswith("https://")
            and not self.session_cookie_secure
        ):
            raise ValueError(
                "AUTH_COOKIE_SECURE must be true when APP_ORIGIN uses HTTPS"
            )
        if self.web_push_test_send_enabled and not self.web_push_enabled:
            raise ValueError("WEB_PUSH_TEST_SEND_ENABLED requires WEB_PUSH_ENABLED")
        if self.web_push_enabled:
            if not math.isfinite(self.web_push_timeout_seconds) or not (
                0 < self.web_push_timeout_seconds <= 30
            ):
                raise ValueError(
                    "WEB_PUSH_TIMEOUT_SECONDS must be greater than 0 and at most 30"
                )
            _validate_vapid_subject(self.web_push_vapid_subject)
            _validate_vapid_key_pair(
                self.web_push_vapid_public_key,
                self.web_push_vapid_private_key.get_secret_value(),
            )
        if not math.isfinite(self.reminder_poll_interval_seconds) or not (
            0 < self.reminder_poll_interval_seconds <= 3_600
        ):
            raise ValueError(
                "REMINDER_POLL_INTERVAL_SECONDS must be greater than 0 "
                "and at most 3600"
            )
        if not 1 <= self.reminder_batch_size <= 1_000:
            raise ValueError(
                "REMINDER_BATCH_SIZE must be between 1 and 1000"
            )
        if not 1 <= self.reminder_sending_stale_seconds <= 86_400:
            raise ValueError(
                "REMINDER_SENDING_STALE_SECONDS must be between 1 and 86400"
            )
        if not isinstance(self.voice_max_upload_bytes, int) or isinstance(
            self.voice_max_upload_bytes, bool
        ):
            raise ValueError("VOICE_MAX_UPLOAD_BYTES must be an integer")
        if not 1 <= self.voice_max_upload_bytes <= MAX_VOICE_UPLOAD_BYTES:
            raise ValueError(
                "VOICE_MAX_UPLOAD_BYTES must be between 1 and "
                f"{MAX_VOICE_UPLOAD_BYTES}"
            )
        if (
            isinstance(self.voice_asr_timeout_seconds, bool)
            or not isinstance(self.voice_asr_timeout_seconds, (int, float))
            or not math.isfinite(self.voice_asr_timeout_seconds)
            or not 0
            < self.voice_asr_timeout_seconds
            <= MAX_VOICE_ASR_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "VOICE_ASR_TIMEOUT_SECONDS must be greater than 0 and at most "
                f"{int(MAX_VOICE_ASR_TIMEOUT_SECONDS)}"
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
        voice_storage_value = read("VOICE_STORAGE_ROOT").strip()
        ffprobe_value = read("FFPROBE_PATH").strip()
        ffmpeg_value = read("FFMPEG_PATH").strip()
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
            web_push_enabled=read_bool("WEB_PUSH_ENABLED", "false"),
            web_push_vapid_public_key=read(
                "WEB_PUSH_VAPID_PUBLIC_KEY"
            ).strip(),
            web_push_vapid_private_key=SecretStr(
                read("WEB_PUSH_VAPID_PRIVATE_KEY").strip()
            ),
            web_push_vapid_subject=read("WEB_PUSH_VAPID_SUBJECT").strip(),
            web_push_test_send_enabled=read_bool(
                "WEB_PUSH_TEST_SEND_ENABLED", "false"
            ),
            web_push_timeout_seconds=float(
                read("WEB_PUSH_TIMEOUT_SECONDS", "10")
            ),
            reminder_worker_enabled=read_bool(
                "REMINDER_WORKER_ENABLED", "false"
            ),
            reminder_poll_interval_seconds=float(
                read("REMINDER_POLL_INTERVAL_SECONDS", "30")
            ),
            reminder_batch_size=int(read("REMINDER_BATCH_SIZE", "100")),
            reminder_sending_stale_seconds=int(
                read("REMINDER_SENDING_STALE_SECONDS", "300")
            ),
            voice_asr_enabled=read_bool("VOICE_ASR_ENABLED", "false"),
            voice_storage_root=(
                Path(voice_storage_value) if voice_storage_value else None
            ),
            voice_max_upload_bytes=int(
                read(
                    "VOICE_MAX_UPLOAD_BYTES",
                    str(DEFAULT_VOICE_MAX_UPLOAD_BYTES),
                )
            ),
            ffprobe_path=Path(ffprobe_value) if ffprobe_value else None,
            ffmpeg_path=Path(ffmpeg_value) if ffmpeg_value else None,
            alibaba_asr_api_url=read(
                "ALIBABA_ASR_API_URL",
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
                "multimodal-generation/generation",
            ).strip(),
            alibaba_api_key=SecretStr(read("ALIBABA_API_KEY").strip()),
            voice_asr_timeout_seconds=float(
                read("VOICE_ASR_TIMEOUT_SECONDS", "30")
            ),
        )
