"""Execution Engine package boundary (docs/03/04).

Owns CREATED→QUEUED staging, durable Outbox delivery,
QUEUED→RUNNING orchestration claim/lease, initial PENDING→READY,
and TOOL Step Attempt starter foundation (READY→RUNNING + StepAttempt STARTED).

Does NOT yet call MCP, resolve secrets, create ToolCall rows, or wire Attempt
start into the Celery claim task (deferred to the MCP Tool runner PR).
"""
