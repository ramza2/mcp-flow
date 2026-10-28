"""Bundled pets sequential E2E OpenAPI must support list→detail handoff."""

from __future__ import annotations

from pathlib import Path

from app.factory.openapi_analyzer import analyze_openapi

ROOT = Path(__file__).resolve().parents[3]
OPENAPI = (
    ROOT
    / "scripts"
    / "ops"
    / "pets-sequential-e2e"
    / "openapi"
    / "pets-sequential-e2e.yaml"
)


def test_bundled_openapi_exposes_list_and_detail_with_typed_id() -> None:
    assert OPENAPI.is_file()
    analysis = analyze_openapi(OPENAPI.read_bytes(), filename=OPENAPI.name)
    by_key = {op.operation_key: op for op in analysis.operations}
    assert "GET /pets" in by_key
    assert "GET /pets/{pet_id}" in by_key

    list_op = by_key["GET /pets"]
    assert list_op.response_schema is not None
    pets = list_op.response_schema["properties"]["pets"]
    assert pets["type"] == "array"
    assert pets["items"]["properties"]["id"]["type"] == "integer"

    detail = by_key["GET /pets/{pet_id}"]
    pet_id = next(p for p in detail.parameters if p.name == "pet_id")
    dumped = pet_id.model_dump()
    assert dumped["required"] is True
    assert dumped["location"] == "path"
    assert (dumped.get("schema") or {}).get("type") == "integer"
