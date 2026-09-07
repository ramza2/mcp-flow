"""Model Provider adapter errors — connection-test classification only.

These are not Canonical Domain status enums.
"""

from __future__ import annotations


class ModelProviderError(Exception):
    """Outbound / protocol failure during a Model Profile connection test."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable


# Connection-test error classification identifiers (ephemeral result only).
NETWORK = "NETWORK"
TIMEOUT = "TIMEOUT"
TLS = "TLS"
AUTH = "AUTH"
HTTP = "HTTP"
PROTOCOL = "PROTOCOL"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
DIMENSION_MISMATCH = "DIMENSION_MISMATCH"
UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
