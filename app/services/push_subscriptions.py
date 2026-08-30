from __future__ import annotations

from app.auth import ValidatedSession
from app.config import Settings
from app.reminder_repository import ReminderRepository
from app.schemas import (
    PushConfigurationPublic,
    PushSubscriptionPublic,
    PushSubscriptionSyncRequest,
    PushSubscriptionStatus,
    PushTestNotificationResponse,
    WebPushOutcome,
)
from app.services.push_security import (
    PushEndpointPolicy,
    validate_subscription_keys,
)
from app.services.web_push import WebPushService, build_test_push_payload


class PushConfigurationUnavailableError(RuntimeError):
    pass


class PushTestSendDisabledError(RuntimeError):
    pass


class PushSubscriptionService:
    def __init__(
        self,
        repository: ReminderRepository,
        settings: Settings,
        web_push_service: WebPushService,
        endpoint_policy: PushEndpointPolicy,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.web_push_service = web_push_service
        self.endpoint_policy = endpoint_policy

    def configuration(self) -> PushConfigurationPublic:
        available = self.settings.web_push_enabled
        return PushConfigurationPublic(
            available=available,
            vapid_public_key=(
                self.settings.web_push_vapid_public_key if available else None
            ),
        )

    def sync(
        self,
        payload: PushSubscriptionSyncRequest,
        validated: ValidatedSession,
    ) -> PushSubscriptionPublic:
        if not self.settings.web_push_enabled:
            raise PushConfigurationUnavailableError("web push is unavailable")
        endpoint = self.endpoint_policy.validate(
            payload.endpoint.get_secret_value()
        )
        p256dh, auth = validate_subscription_keys(
            payload.keys.p256dh.get_secret_value(),
            payload.keys.auth.get_secret_value(),
        )
        record = self.repository.sync_push_subscription(
            user_id=validated.user.id,
            session_id=validated.session.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )
        return PushSubscriptionPublic(id=record.id, status=record.status)

    def revoke(
        self,
        subscription_id: int,
        validated: ValidatedSession,
    ) -> PushSubscriptionPublic:
        record = self.repository.revoke_push_subscription(
            subscription_id,
            validated.user.id,
            validated.session.id,
        )
        return PushSubscriptionPublic(id=record.id, status=record.status)

    def revoke_current(self, validated: ValidatedSession) -> PushSubscriptionPublic:
        record = self.repository.revoke_current_session_push_subscription(
            validated.user.id,
            validated.session.id,
        )
        return PushSubscriptionPublic(id=record.id, status=record.status)

    def send_test_notification(
        self,
        validated: ValidatedSession,
    ) -> PushTestNotificationResponse:
        if not self.settings.web_push_test_send_enabled:
            raise PushTestSendDisabledError("test notification is disabled")
        if not self.settings.web_push_enabled:
            raise PushConfigurationUnavailableError("web push is unavailable")
        subscription = self.repository.get_active_push_subscription_for_session(
            validated.user.id,
            validated.session.id,
        )
        result = self.web_push_service.send(
            subscription,
            build_test_push_payload(),
        )
        if result.outcome == WebPushOutcome.SUBSCRIPTION_GONE:
            self.repository.set_push_subscription_status(
                subscription.id,
                validated.user.id,
                status=PushSubscriptionStatus.INVALID,
                last_error_code=result.error_code,
            )
        return PushTestNotificationResponse(
            outcome=result.outcome,
            provider_status=result.provider_status,
        )
