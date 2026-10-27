"""Unit helpers for Agent Run chat-result projection."""

from __future__ import annotations

from app.services.agent_run_chat_result import _project_pets


def test_project_pets_alpha_beta() -> None:
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


def test_project_pets_rejects_non_pets_shape() -> None:
    assert _project_pets({"items": [{"name": "Alpha", "status": "available"}]}) is None
    assert _project_pets(None) is None
    assert _project_pets({"pets": "nope"}) is None
