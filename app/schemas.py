from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.time_utils import (
    normalize_utc_datetime,
    validate_default_reminder_time,
    validate_timezone_name,
)


class PriorityLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ItemStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    TRASH = "trash"


class InputMethod(str, Enum):
    TEXT = "text"
    VOICE = "voice"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VoiceTranscriptionStatus(str, Enum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VoiceFailureCode(str, Enum):
    CONFIGURATION = "configuration"
    NETWORK = "network"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    QUOTA_RATE_LIMIT = "quota_rate_limit"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_RESPONSE = "invalid_response"
    MEDIA_PROBE = "media_probe"
    UNSUPPORTED_MEDIA = "unsupported_media"
    CONVERSION = "conversion"
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"
    DRAFT_TEXT_LIMIT = "draft_text_limit"


class FailureType(str, Enum):
    CONFIGURATION = "configuration"
    NETWORK = "network"
    API = "api"
    INVALID_OUTPUT = "invalid_output"
    INTERNAL = "internal"


class ReminderStatus(str, Enum):
    NEEDS_CONFIRMATION = "needs_confirmation"
    SCHEDULED = "scheduled"
    DUE = "due"
    CANCELLED = "cancelled"


class ReminderCancelReason(str, Enum):
    USER_CANCELLED = "user_cancelled"
    ITEM_COMPLETED = "item_completed"
    ITEM_TRASHED = "item_trashed"


class PushSubscriptionStatus(str, Enum):
    ACTIVE = "active"
    INVALID = "invalid"
    REVOKED = "revoked"


class WebPushPayloadType(str, Enum):
    REMINDER = "reminder"
    TEST = "test"


class WebPushOutcome(str, Enum):
    ACCEPTED = "accepted"
    SUBSCRIPTION_GONE = "subscription_gone"
    AUTHENTICATION_ERROR = "authentication_error"
    TRANSIENT_ERROR = "transient_error"
    PROVIDER_ERROR = "provider_error"
    CONFIGURATION_ERROR = "configuration_error"


class ReminderDeliveryStatus(str, Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ImportantField(str, Enum):
    IMPORTANCE = "importance"
    URGENCY = "urgency"
    DEADLINE = "deadline"


Deadline = date | datetime


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserCreationRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value: str) -> str:
        value = value.strip()
        if value.count("@") != 1 or any(character.isspace() for character in value):
            raise ValueError("email must be a valid address")
        local_part, domain = value.rsplit("@", 1)
        if (
            not local_part
            or not domain
            or "." not in domain
            or domain.startswith(".")
            or domain.endswith(".")
        ):
            raise ValueError("email must be a valid address")
        return value

    @field_validator("display_name", "timezone")
    @classmethod
    def user_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        return validate_timezone_name(value)


class RegisterRequest(UserCreationRequest):
    invite_code: SecretStr = Field(min_length=1, max_length=256)


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def login_email_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("email must not be blank")
        return value


class UserPublic(StrictModel):
    id: int
    email: str
    display_name: str
    timezone: str
    default_reminder_time: str = "09:00"
    created_time: datetime
    updated_time: datetime


class UserRecord(StrictModel):
    """Internal persistence model. Never use this as an API response model."""

    id: int
    email: str
    password_hash: str
    display_name: str
    timezone: str
    default_reminder_time: str
    status: UserStatus
    password_changed_time: datetime
    created_time: datetime
    updated_time: datetime


class UserSessionRecord(StrictModel):
    """Internal session persistence model. Token hashes are never public."""

    id: int
    user_id: int
    token_hash: str
    csrf_token_hash: str
    created_time: datetime
    last_seen_time: datetime
    expires_time: datetime
    revoked_time: datetime | None
    user_agent: str | None


class PushConfigurationPublic(StrictModel):
    available: bool
    vapid_public_key: str | None = None


class PushSubscriptionKeys(StrictModel):
    p256dh: SecretStr = Field(min_length=1, max_length=256)
    auth: SecretStr = Field(min_length=1, max_length=128)


class PushSubscriptionSyncRequest(StrictModel):
    endpoint: SecretStr = Field(min_length=1, max_length=2_048)
    keys: PushSubscriptionKeys


class PushSubscriptionPublic(StrictModel):
    id: int
    status: PushSubscriptionStatus


class WebPushPayload(StrictModel):
    type: WebPushPayloadType
    title: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=160)
    target_path: str = Field(min_length=1, max_length=200)

    @field_validator("title", "body")
    @classmethod
    def notification_copy_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("notification copy must not be blank")
        return normalized

    @field_validator("target_path")
    @classmethod
    def target_path_must_be_internal(cls, value: str) -> str:
        normalized = value.strip()
        if normalized in {"/", "/capture", "/dashboard", "/account"}:
            return normalized
        raise ValueError("target path must be an internal SelfEcho route")


class PushTestNotificationResponse(StrictModel):
    outcome: WebPushOutcome
    provider_status: int | None = None


class CaptureRequest(StrictModel):
    original_text: str = Field(min_length=1, max_length=10_000)
    input_method: InputMethod = InputMethod.TEXT

    @field_validator("original_text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("original_text must not be blank")
        return value


class ItemInputPublic(StrictModel):
    id: int
    item_id: int | None
    original_text: str
    input_method: InputMethod
    processing_status: ProcessingStatus
    failure_type: FailureType | None = None
    failure_message: str | None = Field(default=None, max_length=500)
    created_time: datetime
    voice_segment_ids: list[int] = Field(default_factory=list)


class VoiceSegmentPublic(StrictModel):
    id: int
    position: int
    client_segment_id: str
    original_size_bytes: int
    client_content_type: str | None
    detected_container: str | None
    detected_codec: str | None
    sample_rate_hz: int | None
    channels: int | None
    duration_ms: int | None
    transcription_status: VoiceTranscriptionStatus
    provider_transcript: str | None
    failure_code: VoiceFailureCode | None
    failure_message: str | None = Field(default=None, max_length=500)
    attempt_count: int
    created_time: datetime
    updated_time: datetime


class CaptureDraftPublic(StrictModel):
    id: int
    current_text: str
    revision: int
    created_time: datetime
    updated_time: datetime
    voice_segments: list[VoiceSegmentPublic] = Field(default_factory=list)


class CaptureDraftResponse(StrictModel):
    draft: CaptureDraftPublic | None
    voice_available: bool = False


class CaptureDraftPutRequest(StrictModel):
    current_text: str = Field(max_length=10_000)
    revision: int = Field(ge=0)


class CaptureDraftSaveRequest(StrictModel):
    draft_id: int = Field(gt=0)
    revision: int = Field(ge=0)


class PersonalItemPublic(StrictModel):
    id: int
    title: str
    type: str
    importance: PriorityLevel
    urgency: PriorityLevel
    deadline: Deadline | None
    estimated_time: int | None
    status: ItemStatus
    next_action: str | None
    extra_information: dict[str, Any] | None
    created_time: datetime
    updated_time: datetime


class ReminderRecord(StrictModel):
    """Internal persistence model for one per-user Reminder history entry."""

    id: int
    user_id: int
    item_id: int
    source_expression: str | None
    scheduled_timezone: str
    remind_at: datetime | None
    status: ReminderStatus
    created_time: datetime
    updated_time: datetime
    due_time: datetime | None
    cancelled_time: datetime | None
    cancel_reason: ReminderCancelReason | None
    surfaced_time: datetime | None


class PushSubscriptionRecord(StrictModel):
    """Internal subscription model. Endpoint and keys are never public."""

    id: int
    user_id: int
    session_id: int
    endpoint: SecretStr
    p256dh: SecretStr
    auth: SecretStr
    status: PushSubscriptionStatus
    created_time: datetime
    updated_time: datetime
    invalidated_time: datetime | None
    last_error_code: str | None


class ReminderDeliveryRecord(StrictModel):
    """Internal durable ledger record for one subscription delivery attempt."""

    id: int
    user_id: int
    reminder_id: int
    subscription_id: int
    status: ReminderDeliveryStatus
    attempted_time: datetime | None
    finished_time: datetime | None
    provider_status: str | None


class ReminderPublic(StrictModel):
    id: int
    item_id: int
    source_expression: str | None
    scheduled_timezone: str
    remind_at: datetime | None
    status: ReminderStatus
    created_time: datetime
    updated_time: datetime
    due_time: datetime | None
    cancelled_time: datetime | None
    cancel_reason: ReminderCancelReason | None
    surfaced_time: datetime | None


class ReminderWithItemPublic(ReminderPublic):
    item_title: str


class DashboardItem(StrictModel):
    id: int
    title: str
    importance: PriorityLevel
    urgency: PriorityLevel
    deadline: Deadline | None
    estimated_time: int | None
    priority_score: float | None = None
    reminder: ReminderPublic | None = None
    show_reminder_prompt: bool = False


class DashboardResponse(StrictModel):
    sortable_items: list[DashboardItem]
    needs_confirmation: list[DashboardItem]
    pending_inputs: list[ItemInputPublic]
    failed_inputs: list[ItemInputPublic]
    due_reminders: list[ReminderWithItemPublic] = Field(default_factory=list)


class ItemDetailResponse(StrictModel):
    item: PersonalItemPublic
    inputs: list[ItemInputPublic]
    reminder: ReminderPublic | None = None
    show_reminder_prompt: bool = False


class ReminderScheduleRequest(StrictModel):
    local_date: date
    local_time: str | None = None

    @field_validator("local_time")
    @classmethod
    def reminder_time_must_be_valid(cls, value: str | None) -> str | None:
        return validate_default_reminder_time(value) if value is not None else None


class ItemReminderResponse(StrictModel):
    reminder: ReminderPublic | None
    show_reminder_prompt: bool


class ReminderPromptResponse(StrictModel):
    dismissed_time: datetime
    show_reminder_prompt: bool = False


class ReminderSettingsPublic(StrictModel):
    timezone: str
    default_reminder_time: str


class ReminderSettingsPatch(StrictModel):
    default_reminder_time: str

    @field_validator("default_reminder_time")
    @classmethod
    def default_time_must_be_valid(cls, value: str) -> str:
        return validate_default_reminder_time(value)


class UserItemPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, min_length=1, max_length=50)
    importance: PriorityLevel | None = None
    urgency: PriorityLevel | None = None
    deadline: Deadline | None = None
    estimated_time: int | None = Field(default=None, ge=1, le=100_800)
    status: ItemStatus | None = None
    next_action: str | None = Field(default=None, max_length=1_000)
    extra_information: dict[str, Any] | None = None
    confirmed_important_fields: bool = False

    @field_validator("title", "type")
    @classmethod
    def non_blank_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class AIItemFields(StrictModel):
    """Validated provider output. Omitted fields mean 'leave unchanged'."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, min_length=1, max_length=50)
    importance: PriorityLevel | None = None
    urgency: PriorityLevel | None = None
    deadline: Deadline | None = None
    estimated_time: int | None = Field(
        default=None,
        ge=1,
        le=100_800,
        strict=True,
    )
    status: ItemStatus | None = None
    next_action: str | None = Field(default=None, max_length=1_000)
    extra_information: dict[str, Any] | None = None


class AIReminderCandidate(StrictModel):
    """Provider-independent reminder intent extracted from the latest input."""

    intent: bool = Field(default=False, strict=True)
    temporal_expression: str | None = Field(default=None, max_length=200)

    @field_validator("temporal_expression")
    @classmethod
    def temporal_expression_must_not_be_blank(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        expression = value.strip()
        return expression or None


class ReminderCreationCandidate(StrictModel):
    """Validated persistence command produced by deterministic parsing."""

    source_expression: str | None = Field(default=None, max_length=200)
    scheduled_timezone: str = Field(min_length=1, max_length=100)
    remind_at: datetime | None = None

    @field_validator("source_expression")
    @classmethod
    def source_expression_must_not_be_blank(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        expression = value.strip()
        return expression or None

    @field_validator("scheduled_timezone")
    @classmethod
    def scheduled_timezone_must_be_valid(cls, value: str) -> str:
        return validate_timezone_name(value)

    @field_validator("remind_at")
    @classmethod
    def remind_at_must_be_canonical_utc(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name="remind_at")


class AIExtraction(StrictModel):
    fields: AIItemFields
    evidence_fields: set[ImportantField] = Field(default_factory=set)
    reminder: AIReminderCandidate = Field(default_factory=AIReminderCandidate)


class MessageResponse(StrictModel):
    message: str
