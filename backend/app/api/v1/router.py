from fastapi import APIRouter

from app.api.v1.agents import router as agents_router
from app.api.v1.mcp_servers import router as mcp_servers_router
from app.api.v1.mcp_tools import router as mcp_tools_router
from app.api.v1.model_profiles import router as model_profiles_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.roles import router as roles_router
from app.api.v1.users import router as users_router

api_v1_router = APIRouter()
api_v1_router.include_router(agents_router)
api_v1_router.include_router(mcp_servers_router)
api_v1_router.include_router(mcp_tools_router)
api_v1_router.include_router(model_profiles_router)
api_v1_router.include_router(users_router)
api_v1_router.include_router(roles_router)
api_v1_router.include_router(permissions_router)
