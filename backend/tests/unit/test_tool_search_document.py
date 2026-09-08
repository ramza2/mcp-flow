"""Unit tests for deterministic ToolSearchDocumentBuilder."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.mcp import MCPTool, MCPToolVersion
from app.search.tool_document import TOOL_SEARCH_DOCUMENT_V1, ToolSearchDocumentBuilder


def _tool(**overrides: Any) -> MCPTool:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "mcp_server_id": uuid.uuid4(),
        "remote_name": "weather_lookup",
        "display_name": "Weather Lookup",
        "description_override": None,
        "tags": ["weather", "read"],
        "status": "DISCOVERED",
        "first_seen_at": now,
        "last_seen_at": now,
        "created_at": now,
        "updated_at": now,
        "lock_version": 1,
    }
    values.update(overrides)
    return MCPTool(**values)


def _version(**overrides: Any) -> MCPToolVersion:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "mcp_tool_id": uuid.uuid4(),
        "version_no": 1,
        "remote_description": "Lookup weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "title": "City",
                    "description": "City name",
                },
                "units": {"type": "string", "description": "Unit system"},
            },
            "required": ["city"],
        },
        "output_schema": {
            "description": "Weather payload",
            "properties": {
                "temp": {"title": "Temperature", "description": "Air temperature"},
            },
        },
        "annotations": {"title": "Weather"},
        "raw_descriptor": {
            "name": "weather_lookup",
            "endpoint": "https://secret.example/mcp",
            "authorization": "Bearer should-not-appear",
        },
        "content_hash": "a" * 64,
        "validation_status": "VALID",
        "discovered_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return MCPToolVersion(**values)


def test_identical_input_identical_document() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool()
    version = _version(mcp_tool_id=tool.id)
    a = builder.build(tool, version)
    b = builder.build(tool, version)
    assert a.search_text == b.search_text
    assert a.content_hash == b.content_hash
    assert len(a.content_hash) == 64
    assert TOOL_SEARCH_DOCUMENT_V1 not in a.search_text


def test_display_name_change_changes_hash() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool(display_name="A")
    version = _version(mcp_tool_id=tool.id)
    base = builder.build(tool, version)
    tool.display_name = "B"
    changed = builder.build(tool, version)
    assert changed.content_hash != base.content_hash
    assert "display_name: B" in changed.search_text


def test_description_override_preferred_and_changes_hash() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool(description_override=None)
    version = _version(mcp_tool_id=tool.id)
    base = builder.build(tool, version)
    assert "Lookup weather for a city" in base.search_text
    tool.description_override = "Operator override"
    changed = builder.build(tool, version)
    assert changed.content_hash != base.content_hash
    assert "Operator override" in changed.search_text
    assert "Lookup weather for a city" not in changed.search_text


def test_tag_order_only_same_hash() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool(tags=["zeta", "alpha", "alpha", "  "])
    version = _version(mcp_tool_id=tool.id)
    a = builder.build(tool, version)
    tool.tags = ["alpha", "zeta"]
    b = builder.build(tool, version)
    assert a.content_hash == b.content_hash
    assert "tags: alpha, zeta" in a.search_text


def test_schema_property_order_only_same_hash() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool()
    version = _version(
        mcp_tool_id=tool.id,
        input_schema={
            "type": "object",
            "properties": {
                "city": {"description": "City name"},
                "units": {"description": "Unit system"},
            },
            "required": ["city"],
        },
    )
    a = builder.build(tool, version)
    version.input_schema = {
        "type": "object",
        "properties": {
            "units": {"description": "Unit system"},
            "city": {"description": "City name"},
        },
        "required": ["city"],
    }
    b = builder.build(tool, version)
    assert a.search_text == b.search_text
    assert a.content_hash == b.content_hash


def test_malformed_schema_does_not_crash() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool()
    version = _version(
        mcp_tool_id=tool.id,
        input_schema="not-a-dict",
        output_schema={"properties": "nope", "description": 12},
    )
    doc = builder.build(tool, version)
    assert "input_field:" not in doc.search_text
    assert "output_field:" not in doc.search_text
    assert doc.content_hash


def test_secrets_and_endpoints_not_embedded() -> None:
    builder = ToolSearchDocumentBuilder()
    tool = _tool()
    version = _version(mcp_tool_id=tool.id)
    doc = builder.build(tool, version)
    forbidden = [
        "https://secret.example/mcp",
        "Bearer should-not-appear",
        "authorization",
        "credential_secret_id",
        "raw_descriptor",
        "endpoint_url",
    ]
    lowered = doc.search_text.casefold()
    for item in forbidden:
        assert item.casefold() not in lowered
    assert "weather_lookup" in doc.search_text
    assert "City name" in doc.search_text
    assert "output_summary: Weather payload" in doc.search_text
    assert "required=true" in doc.search_text
