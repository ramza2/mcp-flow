from __future__ import annotations

import contextvars
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def get_request_id() -> str | None:
    return _request_id_ctx.get()


def set_request_id(request_id: str) -> None:
    _request_id_ctx.set(request_id)


def normalize_request_id(raw: str | None, *, max_length: int) -> str:
    """Reuse a client-provided id when safe; otherwise generate a new UUID4 hex id."""
    if raw:
        candidate = raw.strip()
        if len(candidate) <= max_length and _SAFE_REQUEST_ID.fullmatch(candidate):
            return candidate
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Pure ASGI middleware for X-Request-ID (avoids BaseHTTPMiddleware exception issues)."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        header_name = self._settings.request_id_header
        incoming = _header_value(scope, header_name)
        request_id = normalize_request_id(incoming, max_length=self._settings.request_id_max_length)

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id
        else:
            state.request_id = request_id

        token = _request_id_ctx.set(request_id)
        header_bytes = (
            header_name.lower().encode("latin-1"),
            request_id.encode("latin-1"),
        )

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(header_bytes)
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id_ctx.reset(token)


def _header_value(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return None
