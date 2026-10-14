"""Tool Factory package boundary (docs/03).

OpenAPI analyzer, durable analysis Job/API, Candidate Tools, Build snapshots,
Generated Package artifacts, sandbox Security/Contract TestRuns, and Review.
factory-worker runs analysis and declarative package tests; this package does
not execute generated code or hand off MCP Servers.

Generated Tool ≠ activated Tool. Analysis success ≠ generated Tool.
Review APPROVE ≠ Publish/Import ≠ MCPTool/MCPServer.
"""

from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.openapi_analyzer import analyze_openapi
from app.factory.package_generator import (
    FACTORY_PACKAGE_GENERATOR_VERSION,
    PACKAGE_SCHEMA_VERSION,
    build_generated_package,
)
from app.factory.sandbox_contract import FACTORY_CONTRACT_POLICY_VERSION
from app.factory.sandbox_security import FACTORY_SECURITY_POLICY_VERSION

# Stable analyzer evidence version (Factory-local; not a product-wide enum).
OPENAPI_ANALYZER_VERSION = "openapi-analyzer-v1"

# Build foundation generator contract placeholder (no code generation yet).
FACTORY_BUILD_GENERATOR_VERSION = "factory-build-foundation-v1"

JOB_TYPE_OPENAPI_ANALYZE = "OPENAPI_ANALYZE"
ARTIFACT_TYPE_OPENAPI_ANALYSIS = "OPENAPI_ANALYSIS"
ARTIFACT_TYPE_GENERATED_PACKAGE = "GENERATED_PACKAGE"
ARTIFACT_CONTENT_TYPE_JSON = "application/json"
PHASE_ANALYZE = "ANALYZE"
PHASE_SANDBOX_TEST = "SANDBOX_TEST"
PHASE_REVIEW = "REVIEW"
FACTORY_REVIEW_EVIDENCE_KIND = "FACTORY_REVIEW_EVIDENCE"
FACTORY_REVIEW_EVIDENCE_SCHEMA_VERSION = 1

__all__ = [
    "ARTIFACT_CONTENT_TYPE_JSON",
    "ARTIFACT_TYPE_GENERATED_PACKAGE",
    "ARTIFACT_TYPE_OPENAPI_ANALYSIS",
    "FACTORY_BUILD_GENERATOR_VERSION",
    "FACTORY_CONTRACT_POLICY_VERSION",
    "FACTORY_PACKAGE_GENERATOR_VERSION",
    "FACTORY_REVIEW_EVIDENCE_KIND",
    "FACTORY_REVIEW_EVIDENCE_SCHEMA_VERSION",
    "FACTORY_SECURITY_POLICY_VERSION",
    "FactoryOpenAPIAnalysis",
    "JOB_TYPE_OPENAPI_ANALYZE",
    "OPENAPI_ANALYZER_VERSION",
    "PACKAGE_SCHEMA_VERSION",
    "PHASE_ANALYZE",
    "PHASE_REVIEW",
    "PHASE_SANDBOX_TEST",
    "analyze_openapi",
    "build_generated_package",
]
