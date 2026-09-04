from fastapi import APIRouter

from app.api.v1.mcp_servers import router as mcp_servers_router
from app.api.v1.mcp_tools import router as mcp_tools_router

api_v1_router = APIRouter()
api_v1_router.include_router(mcp_servers_router)
api_v1_router.include_router(mcp_tools_router)
