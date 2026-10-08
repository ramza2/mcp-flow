"""Stable Factory analysis errors (docs/02 FNC-FAC / REQ-FAC).

Messages must never include source bodies, Authorization/header values,
credentials, or large untrusted fragments.
"""

from __future__ import annotations

FACTORY_SOURCE_TOO_LARGE = "FACTORY_SOURCE_TOO_LARGE"
FACTORY_SOURCE_PARSE_ERROR = "FACTORY_SOURCE_PARSE_ERROR"
FACTORY_OPENAPI_STRUCTURE_INVALID = "FACTORY_OPENAPI_STRUCTURE_INVALID"
FACTORY_OPENAPI_VERSION_UNSUPPORTED = "FACTORY_OPENAPI_VERSION_UNSUPPORTED"
FACTORY_EXTERNAL_REF_REJECTED = "FACTORY_EXTERNAL_REF_REJECTED"
FACTORY_SERVER_URL_INVALID = "FACTORY_SERVER_URL_INVALID"
FACTORY_ANALYSIS_LIMIT_EXCEEDED = "FACTORY_ANALYSIS_LIMIT_EXCEEDED"


class FactoryAnalysisError(Exception):
    """Analyzer-level failure with a stable application error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
