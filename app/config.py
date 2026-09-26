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
_TENCENT_REGION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")
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
    # Finite local fallbacks; operator admission policy is set explicitly.
    auth_registration_window_seconds: int = 60
    auth_registration_source_limit: int = 20
    auth_registration_global_limit: int = 100
    auth_login_window_seconds: int = 60
    auth_login_source_limit: int = 30
    auth_login_account_limit: int = 10
    auth_login_global_limit: int = 300
    auth_admission_max_keys: int = 1024
    app_origin: str = "http://127.0.0.1:8000"
    session_expiration_seconds: int = 2_592_000
    session_cookie_secure: bool = False
    ai_provider: str = "deepseek"
    ai_api_url: str = "https://api.openai.com/v1/responses"
    ai_api_key: str = ""
    ai_model: str = ""
    deepseek_api_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    ai_timeout_seconds: float = 30.0
    ai_debug_output: bool = False
    # Finite local fallbacks; operator provider policy is set explicitly.
    ai_admission_window_seconds: int = 60
    ai_admission_call_limit: int = 60
    ai_admission_concurrent_limit: int = 4
    ai_provider_input_max_bytes: int = 1_000_000
    asr_admission_window_seconds: int = 60
    asr_admission_call_limit: int = 30
    asr_admission_concurrent_limit: int = 4
    # Finite local defaults; operator reserve and write policy are explicit.
    storage_min_free_bytes: int = 64 * 1024 * 1024
    storage_write_window_seconds: int = 60
    storage_write_limit: int = 120
    storage_voice_concurrent_limit: int = 4
    storage_item_extra_max_bytes: int = 256 * 1024
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
    trash_retention_poll_interval_seconds: float = 3_600.0
    trash_retention_batch_size: int = 100
    email_reminder_provider_enabled: bool = False
    tencent_ses_region: str = "ap-guangzhou"
    tencentcloud_secret_id: SecretStr = SecretStr("")
    tencentcloud_secret_key: SecretStr = SecretStr("")
    tencent_ses_from_email_address: str = ""
    tencent_ses_verification_template_id: int | None = None
    tencent_ses_reminder_template_id: int | None = None
    tencent_ses_test_template_id: int | None = None
    tencent_ses_timeout_seconds: float = 10.0
    # Finite local fallbacks; operator email policy is set explicitly.
    email_verification_recipient_window_seconds: int = 3_600
    email_verification_recipient_limit: int = 5
    email_verification_recipient_max_keys: int = 1_024
    email_send_window_seconds: int = 60
    email_send_global_limit: int = 60
    email_verification_code_pepper: SecretStr = SecretStr("")
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
        if self.trash_retention_poll_interval_seconds <= 0:
            raise ValueError(
                "TRASH_RETENTION_POLL_INTERVAL_SECONDS must be positive"
            )
        if not 0 < self.trash_retention_batch_size <= 500:
            raise ValueError(
                "TRASH_RETENTION_BATCH_SIZE must be between 1 and 500"
            )
        if self.email_reminder_provider_enabled:
            if not _TENCENT_REGION_PATTERN.fullmatch(self.tencent_ses_region):
                raise ValueError(
                    "TENCENT_SES_REGION must be a non-empty Tencent region name"
                )
            if not math.isfinite(self.tencent_ses_timeout_seconds) or not (
                0 < self.tencent_ses_timeout_seconds <= 60
            ):
                raise ValueError(
                    "TENCENT_SES_TIMEOUT_SECONDS must be greater than 0 "
                    "and at most 60"
                )
            if (
                not self.tencent_ses_from_email_address.strip()
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in self.tencent_ses_from_email_address
                )
            ):
                raise ValueError(
                    "TENCENT_SES_FROM_EMAIL_ADDRESS must be non-empty and "
                    "contain no control characters"
                )
            missing = [
                name
                for name, value in (
                    (
                        "TENCENTCLOUD_SECRET_ID",
                        self.tencentcloud_secret_id.get_secret_value(),
                    ),
                    (
                        "TENCENTCLOUD_SECRET_KEY",
                        self.tencentcloud_secret_key.get_secret_value(),
                    ),
                    (
                        "TENCENT_SES_VERIFICATION_TEMPLATE_ID",
                        self.tencent_ses_verification_template_id,
                    ),
                    (
                        "TENCENT_SES_REMINDER_TEMPLATE_ID",
                        self.tencent_ses_reminder_template_id,
                    ),
                    (
                        "EMAIL_VERIFICATION_CODE_PEPPER",
                        self.email_verification_code_pepper.get_secret_value(),
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Email Reminder provider is enabled but required settings "
                    f"are missing: {', '.join(missing)}"
                )
            for name, value in (
                (
                    "TENCENT_SES_VERIFICATION_TEMPLATE_ID",
                    self.tencent_ses_verification_template_id,
                ),
                (
                    "TENCENT_SES_REMINDER_TEMPLATE_ID",
                    self.tencent_ses_reminder_template_id,
                ),
                ("TENCENT_SES_TEST_TEMPLATE_ID", self.tencent_ses_test_template_id),
            ):
                if value is not None and (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    raise ValueError(f"{name} must be a positive integer")
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

        def read_positive_int(name: str, default: int) -> int:
            try:
                value = int(read(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

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
        email_provider_enabled = read_bool(
            "EMAIL_REMINDER_PROVIDER_ENABLED", "false"
        )
        email_region = "ap-guangzhou"
        email_secret_id = ""
        email_secret_key = ""
        email_from_address = ""
        email_verification_template_id: int | None = None
        email_reminder_template_id: int | None = None
        email_test_template_id: int | None = None
        email_timeout_seconds = 10.0
        email_verification_pepper = ""
        if email_provider_enabled:
            def read_template_id(name: str) -> int | None:
                raw_value = read(name).strip()
                if not raw_value:
                    return None
                value = int(raw_value)
                if value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
                return value

            email_region = read("TENCENT_SES_REGION", "ap-guangzhou").strip()
            email_secret_id = read("TENCENTCLOUD_SECRET_ID").strip()
            email_secret_key = read("TENCENTCLOUD_SECRET_KEY").strip()
            email_from_address = read("TENCENT_SES_FROM_EMAIL_ADDRESS").strip()
            email_verification_template_id = read_template_id(
                "TENCENT_SES_VERIFICATION_TEMPLATE_ID"
            )
            email_reminder_template_id = read_template_id(
                "TENCENT_SES_REMINDER_TEMPLATE_ID"
            )
            email_test_template_id = read_template_id(
                "TENCENT_SES_TEST_TEMPLATE_ID"
            )
            email_timeout_seconds = float(
                read("TENCENT_SES_TIMEOUT_SECONDS", "10")
            )
            email_verification_pepper = read(
                "EMAIL_VERIFICATION_CODE_PEPPER"
            ).strip()
        voice_storage_value = read("VOICE_STORAGE_ROOT").strip()
        ffprobe_value = read("FFPROBE_PATH").strip()
        ffmpeg_value = read("FFMPEG_PATH").strip()
        return cls(
            database_path=(
                Path(database_value) if database_value else default_database_path()
            ),
            registration_mode=registration_mode,
            invite_code_hash=read("AUTH_INVITE_CODE_HASH").strip().lower(),
            auth_registration_window_seconds=read_positive_int(
                "AUTH_REGISTRATION_WINDOW_SECONDS", 60
            ),
            auth_registration_source_limit=read_positive_int(
                "AUTH_REGISTRATION_SOURCE_LIMIT", 20
            ),
            auth_registration_global_limit=read_positive_int(
                "AUTH_REGISTRATION_GLOBAL_LIMIT", 100
            ),
            auth_login_window_seconds=read_positive_int(
                "AUTH_LOGIN_WINDOW_SECONDS", 60
            ),
            auth_login_source_limit=read_positive_int(
                "AUTH_LOGIN_SOURCE_LIMIT", 30
            ),
            auth_login_account_limit=read_positive_int(
                "AUTH_LOGIN_ACCOUNT_LIMIT", 10
            ),
            auth_login_global_limit=read_positive_int(
                "AUTH_LOGIN_GLOBAL_LIMIT", 300
            ),
            auth_admission_max_keys=read_positive_int(
                "AUTH_ADMISSION_MAX_KEYS", 1024
            ),
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
                "DEEPSEEK_MODEL", "deepseek-flash"
            ).strip(),
            ai_timeout_seconds=float(read("AI_TIMEOUT_SECONDS", "30")),
            ai_debug_output=read_bool("AI_DEBUG_OUTPUT", "false"),
            ai_admission_window_seconds=read_positive_int(
                "AI_ADMISSION_WINDOW_SECONDS", 60
            ),
            ai_admission_call_limit=read_positive_int(
                "AI_ADMISSION_CALL_LIMIT", 60
            ),
            ai_admission_concurrent_limit=read_positive_int(
                "AI_ADMISSION_CONCURRENT_LIMIT", 4
            ),
            ai_provider_input_max_bytes=read_positive_int(
                "AI_PROVIDER_INPUT_MAX_BYTES", 1_000_000
            ),
            asr_admission_window_seconds=read_positive_int(
                "ASR_ADMISSION_WINDOW_SECONDS", 60
            ),
            asr_admission_call_limit=read_positive_int(
                "ASR_ADMISSION_CALL_LIMIT", 30
            ),
            asr_admission_concurrent_limit=read_positive_int(
                "ASR_ADMISSION_CONCURRENT_LIMIT", 4
            ),
            storage_min_free_bytes=read_positive_int(
                "STORAGE_MIN_FREE_BYTES", 64 * 1024 * 1024
            ),
            storage_write_window_seconds=read_positive_int(
                "STORAGE_WRITE_WINDOW_SECONDS", 60
            ),
            storage_write_limit=read_positive_int(
                "STORAGE_WRITE_LIMIT", 120
            ),
            storage_voice_concurrent_limit=read_positive_int(
                "STORAGE_VOICE_CONCURRENT_LIMIT", 4
            ),
            storage_item_extra_max_bytes=read_positive_int(
                "STORAGE_ITEM_EXTRA_MAX_BYTES", 256 * 1024
            ),
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
            trash_retention_poll_interval_seconds=float(
                read("TRASH_RETENTION_POLL_INTERVAL_SECONDS", "3600")
            ),
            trash_retention_batch_size=int(
                read("TRASH_RETENTION_BATCH_SIZE", "100")
            ),
            email_reminder_provider_enabled=email_provider_enabled,
            tencent_ses_region=email_region,
            tencentcloud_secret_id=SecretStr(email_secret_id),
            tencentcloud_secret_key=SecretStr(email_secret_key),
            tencent_ses_from_email_address=email_from_address,
            tencent_ses_verification_template_id=email_verification_template_id,
            tencent_ses_reminder_template_id=email_reminder_template_id,
            tencent_ses_test_template_id=email_test_template_id,
            tencent_ses_timeout_seconds=email_timeout_seconds,
            email_verification_recipient_window_seconds=read_positive_int(
                "EMAIL_VERIFICATION_RECIPIENT_WINDOW_SECONDS", 3_600
            ),
            email_verification_recipient_limit=read_positive_int(
                "EMAIL_VERIFICATION_RECIPIENT_LIMIT", 5
            ),
            email_verification_recipient_max_keys=read_positive_int(
                "EMAIL_VERIFICATION_RECIPIENT_MAX_KEYS", 1_024
            ),
            email_send_window_seconds=read_positive_int(
                "EMAIL_SEND_WINDOW_SECONDS", 60
            ),
            email_send_global_limit=read_positive_int(
                "EMAIL_SEND_GLOBAL_LIMIT", 60
            ),
            email_verification_code_pepper=SecretStr(email_verification_pepper),
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
