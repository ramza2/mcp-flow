"""Tool Factory package boundary (docs/03).

OpenAPI analyzer foundation lives here. No Job/API persistence, code generation,
factory-worker execution, or MCP Server handoff in this package yet.

Generated Tool ≠ activated Tool. Analysis success ≠ generated Tool.
"""

from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.openapi_analyzer import analyze_openapi

__all__ = ["FactoryOpenAPIAnalysis", "analyze_openapi"]
