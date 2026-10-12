"""Tool Factory package boundary (docs/03).

OpenAPI analyzer + durable analysis Job/API foundation.
No code generation, factory-worker, or MCP Server handoff yet.

Generated Tool ≠ activated Tool. Analysis success ≠ generated Tool.
"""

from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.openapi_analyzer import analyze_openapi

# Stable analyzer evidence version (Factory-local; not a product-wide enum).
OPENAPI_ANALYZER_VERSION = "openapi-analyzer-v1"

JOB_TYPE_OPENAPI_ANALYZE = "OPENAPI_ANALYZE"
ARTIFACT_TYPE_OPENAPI_ANALYSIS = "OPENAPI_ANALYSIS"
ARTIFACT_CONTENT_TYPE_JSON = "application/json"
PHASE_ANALYZE = "ANALYZE"

__all__ = [
    "ARTIFACT_CONTENT_TYPE_JSON",
    "ARTIFACT_TYPE_OPENAPI_ANALYSIS",
    "FactoryOpenAPIAnalysis",
    "JOB_TYPE_OPENAPI_ANALYZE",
    "OPENAPI_ANALYZER_VERSION",
    "PHASE_ANALYZE",
    "analyze_openapi",
]
