from fastapi import APIRouter

# Feature routers are added in later vertical slices.
# Health lives under /health/* (docs/06/08), not under /api/v1.
api_v1_router = APIRouter()
