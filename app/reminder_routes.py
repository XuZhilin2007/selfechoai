from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.repository import InvalidOperationError, NotFoundError
from app.schemas import (
    ItemReminderResponse,
    ReminderPromptResponse,
    ReminderPublic,
    ReminderScheduleRequest,
    ReminderSettingsPatch,
    ReminderSettingsPublic,
    ReminderWithItemPublic,
    UserPublic,
)
from app.services.reminders import ReminderService


def create_reminder_router(
    service: ReminderService,
    require_current_user: Callable[..., UserPublic],
    require_csrf_current_user: Callable[..., UserPublic],
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["reminders"])

    @router.get("/items/{item_id}/reminder", response_model=ItemReminderResponse)
    def get_item_reminder(
        item_id: int,
        current_user: UserPublic = Depends(require_current_user),
    ) -> ItemReminderResponse:
        service.lazy_transition_due(current_user.id)
        try:
            return service.get_item_state(item_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post(
        "/items/{item_id}/reminder",
        response_model=ReminderPublic,
        status_code=status.HTTP_201_CREATED,
    )
    def create_reminder(
        item_id: int,
        payload: ReminderScheduleRequest,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderPublic:
        try:
            return service.create_reminder(
                item_id,
                current_user.id,
                local_date=payload.local_date,
                local_time=payload.local_time,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post(
        "/items/{item_id}/reminder-prompt/dismiss",
        response_model=ReminderPromptResponse,
    )
    def dismiss_reminder_prompt(
        item_id: int,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderPromptResponse:
        try:
            return service.dismiss_prompt(item_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/reminders/upcoming",
        response_model=list[ReminderWithItemPublic],
    )
    def list_upcoming_reminders(
        current_user: UserPublic = Depends(require_current_user),
    ) -> list[ReminderWithItemPublic]:
        service.lazy_transition_due(current_user.id)
        return service.list_upcoming(current_user.id)

    @router.get("/reminders/due", response_model=list[ReminderWithItemPublic])
    def list_due_reminders(
        unsurfaced_only: bool = Query(default=False),
        current_user: UserPublic = Depends(require_current_user),
    ) -> list[ReminderWithItemPublic]:
        service.lazy_transition_due(current_user.id)
        return service.list_due(
            current_user.id,
            unsurfaced_only=unsurfaced_only,
        )

    @router.put("/reminders/{reminder_id}", response_model=ReminderPublic)
    def reschedule_reminder(
        reminder_id: int,
        payload: ReminderScheduleRequest,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderPublic:
        try:
            return service.reschedule_reminder(
                reminder_id,
                current_user.id,
                local_date=payload.local_date,
                local_time=payload.local_time,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/reminders/{reminder_id}", response_model=ReminderPublic)
    def cancel_reminder(
        reminder_id: int,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderPublic:
        try:
            return service.cancel_reminder(reminder_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/reminders/{reminder_id}/surface", response_model=ReminderPublic)
    def mark_reminder_surfaced(
        reminder_id: int,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderPublic:
        try:
            return service.mark_surfaced(reminder_id, current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/reminder-settings", response_model=ReminderSettingsPublic)
    def get_reminder_settings(
        current_user: UserPublic = Depends(require_current_user),
    ) -> ReminderSettingsPublic:
        try:
            return service.get_settings(current_user.id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/reminder-settings", response_model=ReminderSettingsPublic)
    def update_reminder_settings(
        payload: ReminderSettingsPatch,
        current_user: UserPublic = Depends(require_csrf_current_user),
    ) -> ReminderSettingsPublic:
        try:
            return service.update_default_time(
                current_user.id,
                payload.default_reminder_time,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
