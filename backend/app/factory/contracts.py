"""Internal Tool Factory OpenAPI analysis contracts (not persisted).

Factory-local values only — not new canonical Execution/MCP Domain statuses.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FactorySourceFormat = Literal["JSON", "YAML"]
FactoryIssueSeverity = Literal["ERROR", "WARNING"]
FactoryParameterLocation = Literal["path", "query", "header", "cookie"]


class FactoryAnalysisIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    location: str | None = None
    severity: FactoryIssueSeverity = "ERROR"


class FactoryServerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    description: str | None = None


class FactoryParameterCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    location: FactoryParameterLocation
    required: bool
    schema: dict[str, Any] | None = None


class FactoryOperationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str
    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False
    parameters: list[FactoryParameterCandidate] = Field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None


class FactoryOpenAPIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_sha256: str
    source_format: FactorySourceFormat
    openapi_version: str
    title: str | None = None
    version: str | None = None
    servers: list[FactoryServerCandidate] = Field(default_factory=list)
    operations: list[FactoryOperationCandidate] = Field(default_factory=list)
    issues: list[FactoryAnalysisIssue] = Field(default_factory=list)
