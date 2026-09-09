from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.email_repository import (
    EmailAddressPausedError,
    EmailRateLimitError,
    EmailReminderRepository,
    EmailSettingsRecord,
    EmailSettingsUnavailableError,
    EmailVerificationError,
    normalize_reminder_email,
    utc_now,
)
from app.schemas import (
    EmailDeliveryStatus,
    EmailOperationResponse,
    EmailPauseReason,
    EmailReminderSettingsPublic,
)
from app.services.tencent_ses import (
    EmailProviderUnavailableError,
    EmailSendOutcome,
    EmailSendResult,
    TencentSesEmailSender,
)


logger = logging.getLogger(__name__)


class EmailOperationError(RuntimeError):
    pass


class EmailOperationUnavailableError(EmailOperationError):
    pass


class EmailOperationRateLimitedError(EmailOperationError):
    pass


class EmailOperationConflictError(EmailOperationError):
    pass


class EmailOperationProviderError(EmailOperationError):
    pass


@dataclass(frozen=True, slots=True)
class EmailSweepResult:
    stale_unknown: int = 0
    attempted: int = 0
    accepted: int = 0
    retry_wait: int = 0
    failed: int = 0
    expired: int = 0
    suppressed: int = 0
    ambiguous: int = 0
    status_checks: int = 0


class EmailReminderService:
    def __init__(
        self,
        repository: EmailReminderRepository,
        sender: TencentSesEmailSender,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.settings = settings

    def public_settings(self, user_id: int) -> EmailReminderSettingsPublic:
        return self._public(self.repository.get_settings(user_id))

    def set_address(
        self,
        user_id: int,
        email_address: str,
    ) -> EmailReminderSettingsPublic:
        try:
            record = self.repository.set_candidate_address(
                user_id,
                normalize_reminder_email(email_address),
            )
        except EmailAddressPausedError as exc:
            raise EmailOperationConflictError(
                "该邮箱已暂停，请更换并验证其他邮箱。"
            ) from exc
        except ValueError as exc:
            raise EmailOperationConflictError("邮箱地址格式无效。") from exc
        return self._public(record)

    def set_enabled(
        self,
        user_id: int,
        enabled: bool,
    ) -> EmailReminderSettingsPublic:
        return self._public(self.repository.set_enabled(user_id, enabled))

    def send_verification(self, user_id: int) -> EmailOperationResponse:
        self._require_provider()
        settings = self.repository.get_settings(user_id)
        if settings.email_address is None:
            raise EmailOperationConflictError("请先设置提醒邮箱。")
        if settings.health_status.value == "paused":
            raise EmailOperationConflictError("该邮箱已暂停，请更换其他邮箱。")
        code = generate_verification_code()
        code_hmac = verification_code_hmac(
            self._pepper(),
            user_id,
            settings.email_address,
            code,
        )
        try:
            challenge = self.repository.create_verification_challenge(
                user_id,
                email_address=settings.email_address,
                code_hmac=code_hmac,
            )
        except EmailRateLimitError as exc:
            raise EmailOperationRateLimitedError(str(exc)) from exc
        except (EmailVerificationError, EmailAddressPausedError) as exc:
            raise EmailOperationConflictError(
                "当前邮箱无法发送验证码，请重新检查设置。"
            ) from exc
        try:
            result = self.sender.send_verification(settings.email_address, code)
        except Exception:
            result = EmailSendResult(
                outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                error_code="unexpected_provider_ambiguous",
            )
        self.repository.finish_verification_send(challenge.id, user_id, result)
        if result.pause_destination:
            self.repository.pause_matching_address(
                user_id,
                settings.email_address,
                EmailPauseReason.PROVIDER_SUPPRESSED,
            )
        if result.outcome == EmailSendOutcome.ACCEPTED:
            return EmailOperationResponse(
                message="验证码邮件已提交发送，请检查收件箱或垃圾邮件。",
                settings=self.public_settings(user_id),
            )
        if result.outcome == EmailSendOutcome.AMBIGUOUS_FAILURE:
            raise EmailOperationProviderError(
                "验证码邮件的提交结果不确定，请稍后手动重试。"
            )
        raise EmailOperationProviderError("验证码邮件提交失败，请稍后重试。")

    def confirm_verification(
        self,
        user_id: int,
        code: str,
    ) -> EmailOperationResponse:
        settings = self.repository.get_settings(user_id)
        if settings.email_address is None:
            raise EmailOperationConflictError("验证码无效或已过期。")
        submitted_hmac = verification_code_hmac(
            self._pepper(),
            user_id,
            settings.email_address,
            code,
        )
        try:
            record = self.repository.confirm_verification(
                user_id,
                email_address=settings.email_address,
                submitted_hmac=submitted_hmac,
            )
        except EmailVerificationError as exc:
            raise EmailOperationConflictError("验证码无效或已过期。") from exc
        return EmailOperationResponse(
            message="提醒邮箱已验证。",
            settings=self._public(record),
        )

    def send_test_email(self, user_id: int) -> EmailOperationResponse:
        self._require_provider()
        if not self.sender.test_email_available:
            raise EmailOperationUnavailableError("测试邮件模板尚未配置。")
        try:
            destination = self.repository.reserve_test_send(user_id)
        except EmailRateLimitError as exc:
            raise EmailOperationRateLimitedError(str(exc)) from exc
        except EmailSettingsUnavailableError as exc:
            raise EmailOperationConflictError(
                "需要当前已验证且健康的提醒邮箱。"
            ) from exc
        try:
            result = self.sender.send_test(destination)
        except EmailProviderUnavailableError as exc:
            raise EmailOperationUnavailableError("测试邮件暂不可用。") from exc
        except Exception:
            result = EmailSendResult(
                outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                error_code="unexpected_provider_ambiguous",
            )
        if result.pause_destination:
            self.repository.pause_matching_address(
                user_id,
                destination,
                EmailPauseReason.PROVIDER_SUPPRESSED,
            )
        if result.outcome == EmailSendOutcome.ACCEPTED:
            return EmailOperationResponse(
                message="测试邮件已提交发送，请检查收件箱或垃圾邮件。",
                settings=self.public_settings(user_id),
            )
        if result.outcome == EmailSendOutcome.AMBIGUOUS_FAILURE:
            raise EmailOperationProviderError(
                "测试邮件的提交结果不确定，请稍后手动重试。"
            )
        raise EmailOperationProviderError("测试邮件提交失败，请稍后重试。")

    def run_sweep_once(
        self,
        *,
        now_utc: datetime | None,
        batch_size: int,
        stale_after_seconds: int,
    ) -> EmailSweepResult:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        sweep_time = now_utc or utc_now()
        stale_unknown = self.repository.reconcile_stale_sending(
            as_of=sweep_time,
            stale_after_seconds=stale_after_seconds,
        )
        if not self.sender.available:
            return EmailSweepResult(stale_unknown=stale_unknown)

        counts = {
            "attempted": 0,
            "accepted": 0,
            "retry_wait": 0,
            "failed": 0,
            "expired": 0,
            "suppressed": 0,
            "ambiguous": 0,
            "status_checks": 0,
        }
        for _ in range(batch_size):
            attempt_time = now_utc or utc_now()
            target = self.repository.claim_next_delivery(as_of=attempt_time)
            if target is None:
                break
            counts["attempted"] += 1
            if not self.repository.revalidate_claimed_delivery(
                target.id,
                target.user_id,
                as_of=attempt_time,
            ):
                counts["suppressed"] += 1
                continue
            try:
                result = self.sender.send_reminder(
                    target.destination_email,
                    app_url=f"{self.settings.app_origin}/dashboard",
                    requested_at=attempt_time,
                )
            except Exception as exc:
                logger.error(
                    "Email Reminder transport raised unexpectedly "
                    "delivery_id=%s reminder_id=%s exception_type=%s",
                    target.id,
                    target.reminder_id,
                    type(exc).__name__,
                )
                result = EmailSendResult(
                    outcome=EmailSendOutcome.AMBIGUOUS_FAILURE,
                    error_code="worker_unexpected_ambiguous",
                )
            try:
                terminal = self.repository.finish_delivery_attempt(
                    target,
                    result,
                    finished_at=now_utc or utc_now(),
                )
            except Exception as exc:
                logger.error(
                    "Email Reminder outcome could not be persisted "
                    "delivery_id=%s reminder_id=%s exception_type=%s",
                    target.id,
                    target.reminder_id,
                    type(exc).__name__,
                )
                continue
            key = {
                EmailDeliveryStatus.ACCEPTED: "accepted",
                EmailDeliveryStatus.RETRY_WAIT: "retry_wait",
                EmailDeliveryStatus.FAILED: "failed",
                EmailDeliveryStatus.EXPIRED: "expired",
                EmailDeliveryStatus.SUPPRESSED: "suppressed",
                EmailDeliveryStatus.UNKNOWN: "ambiguous",
            }.get(terminal)
            if key is not None:
                counts[key] += 1

        for _ in range(batch_size):
            target = self.repository.claim_next_status_check(as_of=sweep_time)
            if target is None:
                break
            counts["status_checks"] += 1
            try:
                result = self.sender.get_send_status(
                    provider_message_id=target.provider_message_id,
                    provider_request_date=target.provider_request_date,
                    destination=target.destination_email,
                )
            except Exception as exc:
                logger.error(
                    "Email status query raised unexpectedly delivery_id=%s "
                    "exception_type=%s",
                    target.id,
                    type(exc).__name__,
                )
                from app.services.tencent_ses import (
                    EmailStatusQueryOutcome,
                    EmailStatusResult,
                )

                result = EmailStatusResult(
                    outcome=EmailStatusQueryOutcome.FAILED,
                    error_code="unexpected_status_query_error",
                )
            self.repository.finish_status_check(
                target,
                result,
                checked_at=now_utc or utc_now(),
            )
        return EmailSweepResult(stale_unknown=stale_unknown, **counts)

    def _public(self, record: EmailSettingsRecord) -> EmailReminderSettingsPublic:
        return EmailReminderSettingsPublic(
            email_address=record.email_address,
            verification_status=record.verification_status,
            verified_at=record.verified_at,
            enabled=record.enabled,
            health_status=record.health_status,
            pause_reason=record.pause_reason,
            effective_active=record.effective_active and self.sender.available,
            provider_available=self.sender.available,
            test_email_available=self.sender.test_email_available,
        )

    def _require_provider(self) -> None:
        if not self.sender.available:
            raise EmailOperationUnavailableError("邮件提醒服务尚未配置。")

    def _pepper(self) -> str:
        pepper = self.settings.email_verification_code_pepper.get_secret_value()
        if not pepper:
            raise EmailOperationUnavailableError("邮件验证服务尚未配置。")
        return pepper


def generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def verification_code_hmac(
    pepper: str,
    user_id: int,
    email_address: str,
    code: str,
) -> str:
    context = (
        f"selfecho-email-verification:v1:{user_id}:"
        f"{normalize_reminder_email(email_address)}:{code}"
    )
    return hmac.new(
        pepper.encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
