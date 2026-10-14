"""Map Factory package parameters + request_schema to MCP Tool input_schema.

Parameter Builder reads top-level ``properties`` / ``required`` only. OpenAPI
path/query/header/cookie parameters and requestBody must therefore flatten into
one deterministic JSON Schema object without inventing executable code.
"""

from __future__ import annotations

from typing import Any

from fastapi import status

from app.core.errors import AppError

_BODY_PROPERTY = "body"
_LOCATION_ORDER = ("path", "query", "header", "cookie")


class FactoryImportSchemaError(AppError):
    """Fail-closed schema mapping error for Factory Publish/Import."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="FACTORY_PACKAGE_SCHEMA_INVALID",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )


def build_mcp_callable_input_schema(
    *,
    parameters: list[Any] | None,
    request_schema: Any | None,
) -> dict[str, Any]:
    """Build MCP ToolVersion.input_schema from declarative package fields.

    Rules:
    - Each parameter becomes a top-level property keyed by ``name``
    - ``required=true`` parameters are listed in ``required``
    - Parameter ``schema`` is preserved (default ``{type: string}`` when absent)
    - ``request_schema`` is exposed as property ``body`` (no deep merge)
    - Property name collisions fail closed
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    seen_names: set[str] = set()

    ordered_params = _ordered_parameters(parameters or [])
    for param in ordered_params:
        name = str(param["name"]).strip()
        if not name:
            raise FactoryImportSchemaError(
                "Package parameter name must be a non-empty string."
            )
        if name in seen_names:
            raise FactoryImportSchemaError(
                f"Package parameter name collision: {name!r}."
            )
        if name == _BODY_PROPERTY:
            raise FactoryImportSchemaError(
                "Package parameter name 'body' conflicts with request body mapping."
            )
        seen_names.add(name)
        schema = param.get("schema")
        if schema is None:
            prop_schema: dict[str, Any] = {"type": "string"}
        elif isinstance(schema, dict):
            prop_schema = dict(schema)
        else:
            raise FactoryImportSchemaError(
                f"Package parameter {name!r} schema must be an object when present."
            )
        # Preserve OpenAPI location as non-conflicting metadata annotation.
        prop_schema = {
            **prop_schema,
            "x-mcpflow-param-in": param["location"],
        }
        properties[name] = prop_schema
        if param.get("required") is True:
            required.append(name)

    if request_schema is not None:
        if not isinstance(request_schema, dict):
            raise FactoryImportSchemaError(
                "Package request_schema must be an object when present."
            )
        if _BODY_PROPERTY in seen_names or _BODY_PROPERTY in properties:
            raise FactoryImportSchemaError(
                "Cannot map request_schema: property 'body' already exists."
            )
        properties[_BODY_PROPERTY] = dict(request_schema)
        # requestBody presence maps to a callable body argument; treat as required
        # when a schema is provided (OpenAPI required flag is not snapshotted).
        required.append(_BODY_PROPERTY)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _ordered_parameters(parameters: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in parameters:
        if not isinstance(entry, dict):
            raise FactoryImportSchemaError(
                "Package parameters entries must be objects."
            )
        name = entry.get("name")
        location = entry.get("location")
        if not isinstance(name, str) or not name.strip():
            raise FactoryImportSchemaError(
                "Package parameter name must be a non-empty string."
            )
        if location not in _LOCATION_ORDER:
            raise FactoryImportSchemaError(
                "Package parameter location must be path, query, header, or cookie."
            )
        normalized.append(
            {
                "name": name,
                "location": location,
                "required": bool(entry.get("required", False)),
                "schema": entry.get("schema"),
            }
        )
    location_rank = {loc: idx for idx, loc in enumerate(_LOCATION_ORDER)}
    normalized.sort(key=lambda p: (location_rank[p["location"]], p["name"]))
    return normalized
