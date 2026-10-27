"""Unit helpers for Agent Run chat-result projection."""

from __future__ import annotations

from app.services.agent_run_chat_result import (
    _extract_pets_list,
    _is_factory_runtime_wrapper,
    _project_pets,
    _schema_validation_payload,
)


def test_project_pets_direct_alpha_beta() -> None:
    items = _project_pets(
        {
            "pets": [
                {"id": 1, "name": "Alpha", "status": "available"},
                {"id": 2, "name": "Beta", "status": "pending"},
                {"id": 3, "name": "Gamma"},
            ]
        }
    )
    assert items is not None
    assert [(i.name, i.status) for i in items] == [
        ("Alpha", "available"),
        ("Beta", "pending"),
    ]


def test_project_pets_factory_wrapper_body_pets() -> None:
    items = _project_pets(
        {
            "ok": True,
            "http_status": 200,
            "latency_ms": 12,
            "protocol_version": "2026-07-28",
            "body": {
                "pets": [
                    {"id": 1, "name": "Alpha", "status": "available"},
                    {"id": 2, "name": "Beta", "status": "pending"},
                ]
            },
        }
    )
    assert items is not None
    assert [(i.name, i.status) for i in items] == [
        ("Alpha", "available"),
        ("Beta", "pending"),
    ]


def test_project_pets_rejects_failed_factory_upstream() -> None:
    assert (
        _project_pets(
            {
                "ok": False,
                "http_status": 500,
                "body": {
                    "pets": [{"name": "Alpha", "status": "available"}],
                },
            }
        )
        is None
    )
    assert (
        _extract_pets_list(
            {
                "ok": True,
                "http_status": 404,
                "body": {"pets": [{"name": "Alpha", "status": "available"}]},
            }
        )
        is None
    )


def test_project_pets_rejects_non_pets_shape() -> None:
    assert _project_pets({"items": [{"name": "Alpha", "status": "available"}]}) is None
    assert _project_pets(None) is None
    assert _project_pets({"pets": "nope"}) is None
    assert _project_pets({"ok": True, "http_status": 200, "body": {"items": []}}) is None


def test_factory_wrapper_detection_and_schema_payload() -> None:
    wrapper = {
        "ok": True,
        "http_status": 200,
        "body": {"pets": [{"name": "Alpha", "status": "available"}]},
    }
    assert _is_factory_runtime_wrapper(wrapper) is True
    assert _schema_validation_payload(wrapper) == wrapper["body"]
    assert _schema_validation_payload({"pets": []}) == {"pets": []}
