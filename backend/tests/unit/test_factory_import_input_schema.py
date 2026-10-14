"""Unit tests for Factory parameters → MCP callable input_schema mapping."""

from __future__ import annotations

import pytest
from app.agent.parameter_builder import _validate_input_schema
from app.core.errors import AppError
from app.factory.import_input_schema import build_mcp_callable_input_schema


def test_input_schema_preserves_path_query_and_required_body() -> None:
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
        request_body_required=True,
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


def test_optional_request_body_property_not_required() -> None:
    request_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    schema = build_mcp_callable_input_schema(
        parameters=[
            {
                "name": "q",
                "location": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        request_schema=request_schema,
        request_body_required=False,
    )
    assert "body" in schema["properties"]
    assert schema["properties"]["body"] == request_schema
    assert schema["required"] == ["q"]
    assert "body" not in schema["required"]

    _properties, required = _validate_input_schema(schema)
    assert "body" in _properties
    assert "body" not in required


def test_absent_request_body_omits_body_property() -> None:
    schema = build_mcp_callable_input_schema(
        parameters=[
            {
                "name": "id",
                "location": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        request_schema=None,
        request_body_required=False,
    )
    assert "body" not in schema["properties"]
    assert schema["required"] == ["id"]


def test_input_schema_exact_name_collision_fail_closed() -> None:
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


def test_input_schema_normalized_name_collision_fail_closed() -> None:
    with pytest.raises(AppError) as exc:
        build_mcp_callable_input_schema(
            parameters=[
                {
                    "name": "ID",
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
    assert "normalize" in exc.value.message.lower() or "casefold" in exc.value.message.lower()


def test_no_request_body_allows_query_parameter_named_body() -> None:
    schema = build_mcp_callable_input_schema(
        parameters=[
            {
                "name": "body",
                "location": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        request_schema=None,
    )
    assert set(schema["properties"]) == {"body"}
    assert schema["properties"]["body"]["x-mcpflow-param-in"] == "query"
    assert schema["required"] == ["body"]
    properties, required = _validate_input_schema(schema)
    assert "body" in properties
    assert required == ["body"]


def test_request_body_rejects_parameter_named_body() -> None:
    with pytest.raises(AppError) as exc:
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
    assert exc.value.code == "FACTORY_PACKAGE_SCHEMA_INVALID"


def test_request_body_rejects_parameter_named_BODY_normalized() -> None:
    with pytest.raises(AppError) as exc:
        build_mcp_callable_input_schema(
            parameters=[
                {
                    "name": "BODY",
                    "location": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
            request_schema={"type": "object"},
        )
    assert exc.value.code == "FACTORY_PACKAGE_SCHEMA_INVALID"
