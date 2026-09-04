from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.dependencies import DatabasePingDep
from app.schemas.common import HealthLiveResponse, HealthReadyResponse, ReadinessChecks
from app.services.health import check_readiness

router = APIRouter(tags=["health"])


@router.get("/live", response_model=HealthLiveResponse)
async def live() -> HealthLiveResponse:
    """Liveness — process can accept requests (docs/08)."""
    return HealthLiveResponse(status="ok")


@router.get("/ready", response_model=HealthReadyResponse)
async def ready(database_ping: DatabasePingDep) -> JSONResponse:
    """Readiness — traffic-critical checks (DB). Details stay non-sensitive."""
    checks = await check_readiness(database_ping=database_ping)
    db_status = checks["database"]
    ready_ok = db_status == "ok"
    payload = HealthReadyResponse(
        status="ok" if ready_ok else "unavailable",
        checks=ReadinessChecks(database=db_status),
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )
