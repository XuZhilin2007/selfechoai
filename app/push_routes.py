from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import ValidatedSession
from app.repository import InvalidOperationError, NotFoundError
from app.schemas import (
    PushConfigurationPublic,
    PushSubscriptionPublic,
    PushSubscriptionSyncRequest,
    PushTestNotificationResponse,
    UserPublic,
)
from app.services.push_subscriptions import (
    PushConfigurationUnavailableError,
    PushSubscriptionService,
    PushTestSendDisabledError,
)


def create_push_router(
    service: PushSubscriptionService,
    require_current_user: Callable[..., UserPublic],
    require_csrf_validated_session: Callable[..., ValidatedSession],
) -> APIRouter:
    router = APIRouter(prefix="/api/push", tags=["push subscriptions"])

    @router.get("/config", response_model=PushConfigurationPublic)
    def get_push_configuration(
        _current_user: UserPublic = Depends(require_current_user),
    ) -> PushConfigurationPublic:
        return service.configuration()

    @router.put("/subscriptions", response_model=PushSubscriptionPublic)
    def sync_push_subscription(
        payload: PushSubscriptionSyncRequest,
        validated: ValidatedSession = Depends(require_csrf_validated_session),
    ) -> PushSubscriptionPublic:
        try:
            return service.sync(payload, validated)
        except PushConfigurationUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="system notifications are unavailable",
            ) from exc
        except NotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from exc
        except InvalidOperationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid push subscription",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid push subscription",
            ) from exc

    @router.delete(
        "/subscriptions/current",
        response_model=PushSubscriptionPublic,
    )
    def revoke_current_push_subscription(
        validated: ValidatedSession = Depends(require_csrf_validated_session),
    ) -> PushSubscriptionPublic:
        try:
            return service.revoke_current(validated)
        except NotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="push subscription not found",
            ) from exc

    @router.delete(
        "/subscriptions/{subscription_id}",
        response_model=PushSubscriptionPublic,
    )
    def revoke_push_subscription(
        subscription_id: int,
        validated: ValidatedSession = Depends(require_csrf_validated_session),
    ) -> PushSubscriptionPublic:
        try:
            return service.revoke(subscription_id, validated)
        except NotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="push subscription not found",
            ) from exc

    @router.post("/test", response_model=PushTestNotificationResponse)
    def send_test_notification(
        validated: ValidatedSession = Depends(require_csrf_validated_session),
    ) -> PushTestNotificationResponse:
        try:
            return service.send_test_notification(validated)
        except PushTestSendDisabledError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="test notification is disabled",
            ) from exc
        except PushConfigurationUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="system notifications are unavailable",
            ) from exc
        except NotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no active subscription for current session",
            ) from exc

    return router
