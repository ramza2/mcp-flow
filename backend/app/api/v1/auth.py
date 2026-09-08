"""Auth HTTP routes — Login / Session / CSRF (docs/06 §3)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.dependencies import (
    CurrentPrincipalDep,
    DbSessionDep,
    SettingsDep,
    require_csrf_for_unsafe_request,
)
from app.core.config import Settings
from app.schemas.auth_session import (
    AuthSessionResponse,
    CsrfTokenResponse,
    LoginRequest,
)
from app.services.authentication import AuthenticationService

router = APIRouter(prefix="/auth", tags=["auth"])


def _cookie_samesite(value: str) -> Literal["lax", "strict", "none"]:
    normalized = (value or "lax").strip().lower()
    if normalized in {"lax", "strict", "none"}:
        return normalized  # type: ignore[return-value]
    return "lax"


def _set_session_cookie(response: Response, *, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        httponly=True,
        secure=bool(settings.session_cookie_secure),
        samesite=_cookie_samesite(settings.session_cookie_samesite),
        path="/",
        max_age=int(settings.session_ttl_seconds),
    )


def _clear_session_cookie(response: Response, *, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=bool(settings.session_cookie_secure),
        samesite=_cookie_samesite(settings.session_cookie_samesite),
    )


def _raw_session_token(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(settings.session_cookie_name)


@router.post("/login", response_model=AuthSessionResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: DbSessionDep,
    settings: SettingsDep,
) -> AuthSessionResponse:
    result = await AuthenticationService(session, settings).login(
        username=body.username,
        password=body.password,
    )
    _set_session_cookie(response, raw_token=result.raw_session_token, settings=settings)
    return result.response


@router.get("/session", response_model=AuthSessionResponse)
async def get_session(
    request: Request,
    session: DbSessionDep,
    settings: SettingsDep,
) -> AuthSessionResponse:
    return await AuthenticationService(session, settings).get_session(
        _raw_session_token(request, settings)
    )


@router.get("/csrf", response_model=CsrfTokenResponse)
async def get_csrf(
    principal: CurrentPrincipalDep,
    session: DbSessionDep,
    settings: SettingsDep,
) -> CsrfTokenResponse:
    token = await AuthenticationService(session, settings).issue_csrf(
        session_id=principal.session_id
    )
    return CsrfTokenResponse(csrf_token=token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_csrf_for_unsafe_request)],
)
async def logout(
    principal: CurrentPrincipalDep,
    response: Response,
    session: DbSessionDep,
    settings: SettingsDep,
) -> Response:
    await AuthenticationService(session, settings).logout(session_id=principal.session_id)
    _clear_session_cookie(response, settings=settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
