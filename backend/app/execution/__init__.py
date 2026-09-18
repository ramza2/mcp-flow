"""Execution Engine package boundary (docs/03/04).

Owns CREATED→QUEUED staging, durable Outbox delivery,
QUEUED→RUNNING orchestration claim/lease, initial PENDING→READY,
TOOL Step Attempt starter foundation (READY→RUNNING + StepAttempt STARTED),
and the MCP Tool Runner (StepAttempt STARTED → MCP ``tools/call`` →
ToolCall/StepAttempt/ExecutionStep/Execution terminal transition).

The Agent Runtime does not invoke MCP Tools directly — only this package,
via ``McpToolRunner``, performs the outbound ``tools/call``. MCP stdio
execution stays isolated in ``mcp-worker`` and is out of scope here
(``app.mcp.current.CurrentMCPClient`` handles STREAMABLE_HTTP + CURRENT era
only).
"""
