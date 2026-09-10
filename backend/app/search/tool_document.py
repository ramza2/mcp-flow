"""Deterministic Tool search document builder (docs/04 §6, docs/05 §8.6).

Does not invent capability fields — current MCP Tool models have no canonical
capability column (known limitation until Tool Factory metadata canonicalization).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.models.mcp import MCPTool, MCPToolVersion

# Internal builder algorithm version — not a Domain enum.
TOOL_SEARCH_DOCUMENT_V1 = "TOOL_SEARCH_DOCUMENT_V1"

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ToolSearchDocument:
    search_text: str
    content_hash: str


def _norm_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _WS_RE.sub(" ", value).strip()
    return normalized or None


def normalize_search_tags(tags: Any) -> list[str]:
    """Deterministic tag normalization shared by search documents and STALE checks.

    Tags are whitespace-normalized, casefold-deduped, sorted, and stored in
    casefold form so equivalent casing produces identical search documents.
    """
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in tags:
        text = _norm_text(item)
        if text is None:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    out.sort()
    return out


# Backwards-compatible alias for internal builder use.
_norm_tags = normalize_search_tags


def _schema_property_lines(schema: Any, *, role: str) -> list[str]:
    """Extract deterministic field summaries; fail-safe for malformed schemas."""
    if not isinstance(schema, dict):
        return []
    lines: list[str] = []
    top_desc = _norm_text(schema.get("description"))
    if role == "output" and top_desc:
        lines.append(f"output_summary: {top_desc}")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return lines

    required_raw = schema.get("required")
    required: set[str] = set()
    if isinstance(required_raw, list):
        for item in required_raw:
            if isinstance(item, str) and item:
                required.add(item)

    for name in sorted(properties.keys(), key=lambda s: str(s)):
        if not isinstance(name, str) or not name:
            continue
        prop = properties[name]
        if not isinstance(prop, dict):
            continue
        title = _norm_text(prop.get("title"))
        description = _norm_text(prop.get("description"))
        parts = [f"{role}_field: {name}"]
        if title:
            parts.append(f"title={title}")
        if description:
            parts.append(f"description={description}")
        if role == "input":
            parts.append("required=true" if name in required else "required=false")
        lines.append(" | ".join(parts))
    return lines


class ToolSearchDocumentBuilder:
    """Pure builder: identical Tool + ToolVersion input ⇒ identical search_text/hash."""

    def build(self, tool: MCPTool, version: MCPToolVersion) -> ToolSearchDocument:
        lines: list[str] = []

        remote_name = _norm_text(tool.remote_name) or ""
        if remote_name:
            lines.append(f"remote_name: {remote_name}")

        display_name = _norm_text(tool.display_name)
        if display_name:
            lines.append(f"display_name: {display_name}")

        description = _norm_text(tool.description_override)
        if description is None:
            description = _norm_text(version.remote_description)
        if description:
            lines.append(f"description: {description}")

        tags = _norm_tags(tool.tags)
        if tags:
            lines.append("tags: " + ", ".join(tags))

        # Capability: intentionally omitted — no canonical capability field on MCPTool.
        lines.extend(_schema_property_lines(version.input_schema, role="input"))
        lines.extend(_schema_property_lines(version.output_schema, role="output"))

        search_text = "\n".join(lines)
        payload = {
            "builder": TOOL_SEARCH_DOCUMENT_V1,
            "search_text": search_text,
        }
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return ToolSearchDocument(search_text=search_text, content_hash=content_hash)
