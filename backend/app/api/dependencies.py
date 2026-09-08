from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import CurrentPrincipal
from app.core.config import Settings
from app.db.session import get_db_session
from app.mcp.client import MCPHttpClient
from app.services.authentication import AuthenticationService
from app.services.health import DatabasePing, ping_database


def get_app_settings(request: Request) -> Settings:
    """Return settings bound to this application instance (create_app injection)."""
    return request.app.state.settings


def get_request_id_dep(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
RequestIdDep = Annotated[str, Depends(get_request_id_dep)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


async def get_database_ping() -> DatabasePing:
    """Default readiness DB ping — override in tests."""
    return ping_database


DatabasePingDep = Annotated[DatabasePing, Depends(get_database_ping)]


async def get_mcp_http_client() -> AsyncIterator[MCPHttpClient]:
    """Request-scoped MCP HTTP client (facade creates CurrentMCPClient)."""
    client = MCPHttpClient()
    try:
        yield client
    finally:
        await client.aclose()


MCPHttpClientDep = Annotated[MCPHttpClient, Depends(get_mcp_http_client)]


async def get_current_principal(
    request: Request,
    session: DbSessionDep,
    settings: SettingsDep,
) -> CurrentPrincipal:
    """Resolve CurrentPrincipal from Session cookie (no X-User-ID trust)."""
    raw = request.cookies.get(settings.session_cookie_name)
    return await AuthenticationService(session, settings).resolve_principal(raw)


async def require_authenticated_session(
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
) -> CurrentPrincipal:
    return principal


CurrentPrincipalDep = Annotated[
    CurrentPrincipal, Depends(require_authenticated_session)
]


async def require_csrf_for_unsafe_request(
    request: Request,
    principal: CurrentPrincipalDep,
    session: DbSessionDep,
    settings: SettingsDep,
) -> None:
    """CSRF check for unsafe methods when Cookie Session is used.

    Login is exempt (no Session yet). Mount on endpoints that opt in
    (e.g. POST /auth/logout). Existing Agent/MCP routers are not locked yet.
    """
    method = request.method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return
    provided = request.headers.get(settings.csrf_header_name)
    await AuthenticationService(session, settings).require_csrf(
        session_id=principal.session_id,
        provided_token=provided,
    )
