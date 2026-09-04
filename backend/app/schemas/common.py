from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """docs/06 error contract body."""

    code: str
    message: str
    details: list[Any] = Field(default_factory=list)
    request_id: str
    retryable: bool = False


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthLiveResponse(BaseModel):
    status: str = "ok"


class ReadinessChecks(BaseModel):
    database: str


class HealthReadyResponse(BaseModel):
    status: str
    checks: ReadinessChecks
