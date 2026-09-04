from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.middleware import get_request_id
from app.schemas.common import ErrorBody, ErrorResponse


class AppError(Exception):
    """Structured application error mapped to the docs/06 error contract."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: list[Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or []
        self.retryable = retryable


def _error_payload(
    *,
    code: str,
    message: str,
    request_id: str | None,
    details: list[Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            request_id=request_id or get_request_id() or "-",
            retryable=retryable,
        )
    )
    return body.model_dump()


def _resolve_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or get_request_id() or "-"


def _request_id_headers(request: Request, request_id: str) -> dict[str, str]:
    """Attach X-Request-ID when ServerErrorMiddleware bypasses user middleware send()."""
    settings = getattr(request.app.state, "settings", None)
    header_name = getattr(settings, "request_id_header", None) or "X-Request-ID"
    return {header_name: request_id}


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[Any] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    request_id = _resolve_request_id(request)
    return JSONResponse(
        status_code=status_code,
        content=_error_payload(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
            retryable=retryable,
        ),
        headers=_request_id_headers(request, request_id),
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            retryable=exc.retryable,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed"
        code = "HTTP_ERROR" if exc.status_code != 404 else "NOT_FOUND"
        return _error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            details=[],
            retryable=False,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Safe, structured validation details only — no secrets/stack traces.
        details = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": err.get("msg"),
                "type": err.get("type"),
            }
            for err in exc.errors()
        ]
        return _error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details=details,
            retryable=False,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never expose internal exception strings / stack traces to clients.
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            details=[],
            retryable=False,
        )
