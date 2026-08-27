from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.auth import (
    AuthenticationResult,
    AuthenticationService,
    DuplicateEmailError,
    InvalidCredentialsError,
    InvalidCsrfTokenError,
    InvalidInviteCodeError,
    InvalidSessionError,
    RegistrationClosedError,
)
from app.config import Settings
from app.schemas import LoginRequest, RegisterRequest, UserPublic


SESSION_COOKIE_NAME = "__Host-selfecho_session"
LOCAL_SESSION_COOKIE_NAME = "selfecho_session"
CSRF_COOKIE_NAME = "selfecho_csrf"


def create_auth_router(
    service: AuthenticationService, settings: Settings
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["authentication"])

    @router.post(
        "/register",
        response_model=UserPublic,
        status_code=status.HTTP_201_CREATED,
    )
    def register(
        payload: RegisterRequest, request: Request, response: Response
    ) -> UserPublic:
        _validate_request_origin(request, settings)
        try:
            result = service.register(
                invite_code=payload.invite_code.get_secret_value(),
                email=payload.email,
                password=payload.password.get_secret_value(),
                display_name=payload.display_name,
                timezone_name=payload.timezone,
                user_agent=request.headers.get("user-agent"),
            )
        except RegistrationClosedError as exc:
            raise HTTPException(status_code=403, detail="registration is closed") from exc
        except InvalidInviteCodeError as exc:
            raise HTTPException(status_code=403, detail="invalid invite code") from exc
        except DuplicateEmailError as exc:
            raise HTTPException(status_code=409, detail="email is already registered") from exc
        _set_auth_cookies(response, result, settings)
        return result.user

    @router.post("/login", response_model=UserPublic)
    def login(
        payload: LoginRequest, request: Request, response: Response
    ) -> UserPublic:
        _validate_request_origin(request, settings)
        try:
            result = service.login(
                email=payload.email,
                password=payload.password.get_secret_value(),
                user_agent=request.headers.get("user-agent"),
            )
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid email or password",
            ) from exc
        _set_auth_cookies(response, result, settings)
        return result.user

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request) -> Response:
        _validate_request_origin(request, settings)
        session_token = _require_session_cookie(request, settings)
        try:
            service.logout(
                session_token,
                request.headers.get("X-CSRF-Token"),
            )
        except InvalidCsrfTokenError as exc:
            raise HTTPException(status_code=403, detail="invalid CSRF token") from exc
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from exc
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _clear_auth_cookies(response, settings)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/me", response_model=UserPublic)
    def current_user(request: Request, response: Response) -> UserPublic:
        session_token = _require_session_cookie(request, settings)
        try:
            user = service.resolve_current_user(session_token)
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from exc
        response.headers["Cache-Control"] = "no-store"
        return user

    return router


def create_current_user_dependency(
    service: AuthenticationService,
    settings: Settings,
    *,
    csrf_protected: bool = False,
) -> Callable[[Request], UserPublic]:
    """Build a request dependency without coupling business routes to cookies."""

    def current_user(request: Request) -> UserPublic:
        session_token = _require_session_cookie(request, settings)
        try:
            validated = service.validate_session(session_token)
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from exc
        if csrf_protected:
            try:
                service.validate_csrf_token(
                    validated,
                    request.headers.get("X-CSRF-Token"),
                )
            except InvalidCsrfTokenError as exc:
                raise HTTPException(
                    status_code=403,
                    detail="invalid CSRF token",
                ) from exc
        return validated.public_user

    current_user.__name__ = (
        "require_csrf_current_user"
        if csrf_protected
        else "require_current_user"
    )
    return current_user


def session_cookie_name(settings: Settings) -> str:
    return (
        SESSION_COOKIE_NAME
        if settings.session_cookie_secure
        else LOCAL_SESSION_COOKIE_NAME
    )


def _require_session_cookie(request: Request, settings: Settings) -> str:
    session_token = request.cookies.get(session_cookie_name(settings))
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    return session_token


def _validate_request_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != settings.app_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="request origin is not allowed")


def _set_auth_cookies(
    response: Response,
    result: AuthenticationResult,
    settings: Settings,
) -> None:
    common = {
        "secure": settings.session_cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_expiration_seconds,
        "expires": result.expires_time,
    }
    response.set_cookie(
        session_cookie_name(settings),
        result.session_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        result.csrf_token,
        httponly=False,
        **common,
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite="lax",
    )
