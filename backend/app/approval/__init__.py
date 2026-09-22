"""Approval wait foundation package (FNC-EXE-009 / FNC-APR-002)."""

from app.approval.context import (
    APPROVAL_CONTEXT_SCHEMA_VERSION,
    build_approval_context_snapshot,
    compute_approval_context_hash,
)
from app.approval.wait import ApprovalWaitOutcome, ApprovalWaitService

__all__ = [
    "APPROVAL_CONTEXT_SCHEMA_VERSION",
    "ApprovalWaitOutcome",
    "ApprovalWaitService",
    "build_approval_context_snapshot",
    "compute_approval_context_hash",
]
