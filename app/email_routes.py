from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas import (
    EmailAddressRequest,
    EmailOperationResponse,
    EmailReminderEnabledPatch,
    EmailReminderSettingsPublic,
    EmailVerificationCodeRequest,
    UserPublic,
)
from app.services.email_reminders import (
    EmailOperationConflictError,
    EmailOperationProviderError,
    EmailOperationRateLimitedError,
    EmailOperationUnavailableError,
    EmailReminderService,
)


def _rate_limited_response(
    exc: EmailOperationRateLimitedError,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=str(exc),
        headers=(
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None else None
        ),
    )


def create_email_reminder_router(
    service: EmailReminderService,
    require_current_user: Callable[..., UserPublic],
    require_csrf_current_user: Callable[..., UserPublic],
) -> APIRouter:
    router = APIRouter(prefix="/api/email-reminders", tags=["email reminders"])

    @router.get("/settings", response_model=EmailReminderSettingsPublic)
    def get_settings(
        current_user: UserPublic = Depends(require_current_user),
    ) -> EmailReminderSettingsPublic:
        return service.public_settings(current_user.id)

    @router.put("/address", response_model=EmailReminderSettingsPublic)
    def set_address(
        payload: EmailAddressRequest,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> EmailReminderSettingsPublic:
        try:
            return service.set_address(current_user.id, payload.email_address)
        except EmailOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/verification/send", response_model=EmailOperationResponse)
    def send_verification(
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> EmailOperationResponse:
        try:
            return service.send_verification(current_user.id)
        except EmailOperationUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except EmailOperationRateLimitedError as exc:
            raise _rate_limited_response(exc) from exc
        except EmailOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EmailOperationProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/verification/confirm", response_model=EmailOperationResponse)
    def confirm_verification(
        payload: EmailVerificationCodeRequest,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> EmailOperationResponse:
        try:
            return service.confirm_verification(current_user.id, payload.code)
        except EmailOperationUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except EmailOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/enabled", response_model=EmailReminderSettingsPublic)
    def set_enabled(
        payload: EmailReminderEnabledPatch,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> EmailReminderSettingsPublic:
        return service.set_enabled(current_user.id, payload.enabled)

    @router.post("/test", response_model=EmailOperationResponse)
    def send_test_email(
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> EmailOperationResponse:
        try:
            return service.send_test_email(current_user.id)
        except EmailOperationUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except EmailOperationRateLimitedError as exc:
            raise _rate_limited_response(exc) from exc
        except EmailOperationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EmailOperationProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
