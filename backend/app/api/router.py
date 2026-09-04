from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.router import api_v1_router

# Root API aggregator used by create_app.
# - /health/* → Traefik health route (docs/08)
# - /api/v1/* → versioned control-plane API (docs/06)
api_router = APIRouter()
api_router.include_router(health_router, prefix="/health")
api_router.include_router(api_v1_router, prefix="/api/v1")
