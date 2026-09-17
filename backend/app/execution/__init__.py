"""Execution Engine package boundary (docs/03/04).

PR #27 foundation owns CREATED→QUEUED staging, durable Outbox delivery,
QUEUED→RUNNING orchestration claim/lease, and initial PENDING→READY.
Tool execution, StepAttempt, approval/MRTR, retry, cancel, and completion remain deferred.
"""
