"""Agent Runtime package boundary (docs/03/04).

Agent Runtime analyzes / retrieves / selects / plans / validates.
It must NOT invoke MCP Tools directly — Execution Engine owns tool calls.
"""

from app.agent.request_analyzer import RequestAnalyzerService

__all__ = ["RequestAnalyzerService"]
