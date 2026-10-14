"""Deterministic mock upstream for Factory runtime pilot / E2E (GET /pets)."""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="MCPFlow Factory Mock Upstream")


@app.get("/health/live")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/pets")
async def list_pets(
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    pets = [
        {"id": 1, "name": "Alpha", "status": "available"},
        {"id": 2, "name": "Beta", "status": "pending"},
    ]
    if status is not None:
        pets = [p for p in pets if p["status"] == status]
    if limit is not None:
        pets = pets[: max(0, int(limit))]
    return JSONResponse(content={"pets": pets})


@app.post("/pets")
async def create_pet(body: dict) -> JSONResponse:
    return JSONResponse(status_code=201, content={"id": 99, "pet": body})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "app.entrypoints.factory_mock_upstream:app",
        host="0.0.0.0",
        port=8091,
        factory=False,
    )


if __name__ == "__main__":
    main()
