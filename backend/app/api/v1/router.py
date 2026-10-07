from fastapi import APIRouter, Depends

from app.api.dependencies import require_authenticated_api_request
from app.api.v1.agent_requests import router as agent_requests_router
from app.api.v1.agents import router as agents_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.executions import router as executions_router
from app.api.v1.executions import sse_router as executions_sse_router
from app.api.v1.mcp_discovery import router as mcp_discovery_router
from app.api.v1.mcp_servers import router as mcp_servers_router
from app.api.v1.mcp_tools import router as mcp_tools_router
from app.api.v1.model_profiles import router as model_profiles_router
from app.api.v1.ops import router as ops_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.roles import router as roles_router
from app.api.v1.schedules import router as schedules_router
from app.api.v1.users import router as users_router
from app.api.v1.workflows import router as workflows_router

api_v1_router = APIRouter()
api_v1_router.include_router(auth_router)

# Application APIs require Session authentication (+ CSRF on unsafe methods).
# Endpoint-level Permission / ResourceGrant enforcement is deferred.
protected_router = APIRouter(
    dependencies=[Depends(require_authenticated_api_request)],
)
protected_router.include_router(agents_router)
protected_router.include_router(agent_requests_router)
protected_router.include_router(approvals_router)
protected_router.include_router(audit_router)
protected_router.include_router(executions_router)
protected_router.include_router(mcp_servers_router)
protected_router.include_router(mcp_tools_router)
protected_router.include_router(mcp_discovery_router)
protected_router.include_router(model_profiles_router)
protected_router.include_router(ops_router)
protected_router.include_router(workflows_router)
protected_router.include_router(schedules_router)
protected_router.include_router(users_router)
protected_router.include_router(roles_router)
protected_router.include_router(permissions_router)

api_v1_router.include_router(protected_router)
# SSE auth is function-scoped on the route itself; keep it off protected_router
# so request-scoped require_authenticated_api_request cannot leak a DB session
# across the stream lifetime.
api_v1_router.include_router(executions_sse_router)
