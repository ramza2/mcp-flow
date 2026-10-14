"""Unit tests for Factory parameters → MCP callable input_schema mapping."""

from __future__ import annotations

import pytest
from app.agent.parameter_builder import _validate_input_schema
from app.core.errors import AppError
from app.factory.import_input_schema import build_mcp_callable_input_schema


def test_input_schema_preserves_path_query_and_body() -> None:
    parameters = [
        {
            "name": "petId",
            "location": "path",
            "required": True,
            "schema": {"type": "integer", "format": "int64"},
        },
        {
            "name": "verbose",
            "location": "query",
            "required": False,
            "schema": {"type": "boolean"},
        },
        {
            "name": "q",
            "location": "query",
            "required": True,
            "schema": {"type": "string"},
        },
    ]
    request_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    schema = build_mcp_callable_input_schema(
        parameters=parameters,
        request_schema=request_schema,
    )
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"petId", "q", "verbose", "body"}
    assert schema["properties"]["petId"]["type"] == "integer"
    assert schema["properties"]["petId"]["x-mcpflow-param-in"] == "path"
    assert schema["properties"]["q"]["x-mcpflow-param-in"] == "query"
    assert schema["properties"]["verbose"]["x-mcpflow-param-in"] == "query"
    assert schema["properties"]["body"] == request_schema
    assert schema["required"] == ["petId", "q", "body"]
    assert "verbose" not in schema["required"]

    properties, required = _validate_input_schema(schema)
    assert "petId" in properties
    assert "q" in properties
    assert "verbose" in properties
    assert "body" in properties
    assert required == ["petId", "q", "body"]


def test_input_schema_name_collision_fail_closed() -> None:
    with pytest.raises(AppError) as exc:
        build_mcp_callable_input_schema(
            parameters=[
                {
                    "name": "id",
                    "location": "path",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "id",
                    "location": "query",
                    "required": False,
                    "schema": {"type": "string"},
                },
            ],
            request_schema=None,
        )
    assert exc.value.code == "FACTORY_PACKAGE_SCHEMA_INVALID"

    with pytest.raises(AppError) as exc2:
        build_mcp_callable_input_schema(
            parameters=[
                {
                    "name": "body",
                    "location": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
            request_schema={"type": "object"},
        )
    assert exc2.value.code == "FACTORY_PACKAGE_SCHEMA_INVALID"
