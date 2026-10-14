"""Canonical `factory-runtime` process: Current MCP surface for RuntimeBindings."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.secret_crypto import load_master_key_from_settings
from app.core.secrets import DatabaseSecretResolver, UnimplementedSecretResolver
from app.db.session import dispose_db, get_session_factory, init_db
from app.domain.enums import MCPAuthType
from app.factory.runtime_limits import DEFAULT_MAX_CONCURRENCY
from app.factory.runtime_mcp import (
    FactoryRuntimeMcpHandler,
    http_status_for_unavailable,
    mcp_endpoint_path,
)
from app.mcp.auth_headers import build_mcp_auth_headers
from app.services.factory_runtime import FactoryRuntimeService

logger = logging.getLogger(__name__)

_CALL_SEMAPHORE = asyncio.Semaphore(DEFAULT_MAX_CONCURRENCY)


def _secret_resolver(session: Any) -> Any:
    settings = get_settings()
    if settings.secret_master_key_file:
        return DatabaseSecretResolver(
            session,
            master_key_loader=lambda: load_master_key_from_settings(
                file_path=settings.secret_master_key_file
            ),
        )
    return UnimplementedSecretResolver()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    init_db(settings)
    logger.info("factory-runtime started public_base=%s", settings.factory_runtime_public_base_url)
    try:
        yield
    finally:
        await dispose_db()


app = FastAPI(title="MCPFlow Factory Runtime", lifespan=lifespan)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route(
    "/factory-runtime/{binding_id}/mcp",
    methods=["POST"],
)
async def factory_runtime_mcp(binding_id: uuid.UUID, request: Request) -> Response:
    factory = get_session_factory()
    if factory is None:
        return JSONResponse(
            status_code=503, content={"detail": "Database not initialized"}
        )
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
        )

    async with factory() as session:
        svc = FactoryRuntimeService(session)
        try:
            row, contracts = await svc.load_handler_context(binding_id)
        except AppError as exc:
            return JSONResponse(
                status_code=http_status_for_unavailable(),
                content={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": -32004,
                        "message": exc.message,
                        "data": {"code": exc.code},
                    },
                },
            )

        auth_headers: dict[str, str] = {}
        if row.auth_type != MCPAuthType.NONE.value:
            resolver = _secret_resolver(session)
            resolved = None
            if row.auth_secret_id is not None:
                try:
                    resolved = await resolver.resolve(row.auth_secret_id)
                except Exception:
                    resolved = None
            try:
                auth_headers = build_mcp_auth_headers(
                    auth_type=row.auth_type, resolved=resolved
                )
            except AppError as exc:
                return JSONResponse(
                    status_code=409,
                    content={
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "error": {
                            "code": -32001,
                            "message": exc.message,
                            "data": {"code": exc.code},
                        },
                    },
                )

        settings = get_settings()
        allowlist = [
            part.strip()
            for part in (settings.factory_runtime_outbound_host_allowlist or "").split(
                ","
            )
            if part.strip()
        ]
        handler = FactoryRuntimeMcpHandler(
            binding_id=row.id,
            status_value=row.status,
            base_url=row.base_url,
            contracts_by_name=contracts,
            auth_headers=auth_headers or None,
            connect_timeout_ms=int(row.connect_timeout_ms),
            call_timeout_ms=int(row.call_timeout_ms),
            max_response_bytes=int(row.max_response_bytes),
            hostname_allowlist=allowlist or None,
        )
        async with _CALL_SEMAPHORE:
            result = await handler.handle(payload)
        # Never expose secrets; response is JSON-RPC only.
        return JSONResponse(content=result)


@app.api_route("/factory-runtime/{binding_id}/mcp", methods=["GET", "PUT", "PATCH", "DELETE"])
async def factory_runtime_mcp_method_not_allowed(binding_id: uuid.UUID) -> Response:
    del binding_id
    return Response(status_code=405)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    # Document path shape for operators (no dynamic Docker).
    logger.info(
        "factory-runtime MCP path template=%s",
        mcp_endpoint_path(uuid.UUID(int=0)).replace(
            "00000000-0000-0000-0000-000000000000", "{binding_id}"
        ),
    )
    uvicorn.run(
        "app.entrypoints.factory_runtime:app",
        host="0.0.0.0",
        port=8090,
        factory=False,
    )


if __name__ == "__main__":
    main()
