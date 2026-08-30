from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.time_utils import validate_timezone_name


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


class DashboardItem(StrictModel):
    id: int
    title: str
    importance: PriorityLevel
    urgency: PriorityLevel
    deadline: Deadline | None
    estimated_time: int | None
    priority_score: float | None = None


class DashboardResponse(StrictModel):
    sortable_items: list[DashboardItem]
    needs_confirmation: list[DashboardItem]
    pending_inputs: list[ItemInputPublic]
    failed_inputs: list[ItemInputPublic]


class ItemDetailResponse(StrictModel):
    item: PersonalItemPublic
    inputs: list[ItemInputPublic]


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


class AIExtraction(StrictModel):
    fields: AIItemFields
    evidence_fields: set[ImportantField] = Field(default_factory=set)


class MessageResponse(StrictModel):
    message: str
