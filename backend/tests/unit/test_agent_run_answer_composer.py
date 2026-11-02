"""Grounded Agent answer composition contracts (no live Model Provider)."""

from __future__ import annotations

import json
import uuid

from app.services.agent_run_answer_composer import (
    _build_messages,
    _fallback_text,
    _validated_answer,
)
from app.services.agent_run_chat_result import (
    AgentRunChatResult,
    ChatDisplayItem,
    _schema_declared_evidence,
)


def _result(*, evidence=None, available=True):
    return AgentRunChatResult(
        agent_request_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        execution_status="SUCCEEDED",
        available=available,
        unavailable_reason=None if available else "SCHEMA_INVALID",
        tool_name="GET /pets/{id}",
        display_items=[ChatDisplayItem(name="Alpha", status="available")],
        summary_text="조회 결과 1건",
        truncated=False,
        safe_evidence=evidence,
    )


def test_answer_prompt_uses_only_bounded_safe_evidence() -> None:
    result = _result(evidence={"id": 1, "name": "Alpha", "status": "available"})
    messages = _build_messages(
        question="ID 1인 반려동물 상세정보를 알려줘", result=result,
    )
    assert messages[0]["role"] == "system"
    assert "Do not infer breed, lifespan" in messages[0]["content"]
    data = json.loads(messages[1]["content"])
    assert data["evidence"] == {"id": 1, "name": "Alpha", "status": "available"}
    assert "GET /pets/{id}" not in messages[1]["content"]


def test_answer_prompt_falls_back_to_allowlisted_rows_when_evidence_too_large() -> None:
    result = _result(evidence={"description": "X" * 10_000})
    messages = _build_messages(question="조회", result=result)
    data = json.loads(messages[1]["content"])
    assert data["evidence"] == [{"name": "Alpha", "status": "available"}]
    assert "X" * 100 not in messages[1]["content"]


def test_schema_projection_strips_sensitive_keys_and_undeclared_payload() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "breed": {"type": "string"},
            "password": {"type": "string"},
            "details": {
                "type": "object",
                "properties": {
                    "lifespan_years": {"type": "integer"},
                    "api_key": {"type": "string"},
                },
            },
        },
    }
    data = {
        "id": 1, "name": "Alpha", "breed": "Golden Retriever",
        "password": "DONT_SEND", "unknown": "DONT_SEND",
        "details": {"lifespan_years": 12, "api_key": "DONT_SEND"},
    }
    safe = _schema_declared_evidence(data, schema)
    assert safe == {
        "id": 1,
        "name": "Alpha",
        "breed": "Golden Retriever",
        "details": {"lifespan_years": 12},
    }
    assert "DONT_SEND" not in str(safe)


def test_schema_projection_fails_closed_without_declared_properties() -> None:
    assert _schema_declared_evidence(
        {"raw": "private"}, {"type": "object"},
    ) is None
    assert _schema_declared_evidence(
        {"password": "[REDACTED]"},
        {"type": "object", "properties": {"password": {"type": "string"}}},
    ) is None
    assert _schema_declared_evidence(
        {"name": "Alpha"},
        {"$ref": "#/definitions/pet"},
    ) is None


def test_answer_validation_rejects_oversize_and_redacted_content() -> None:
    assert _validated_answer({"answer": "Alpha는 조회된 반려동물입니다."}) == (
        "Alpha는 조회된 반려동물입니다."
    )
    assert _validated_answer({"answer": ""}) is None
    assert _validated_answer({"answer": "[REDACTED]"}) is None
    assert _validated_answer({"answer": "X" * 2_000}) is None
    assert _validated_answer({"wrong": "Alpha"}) is None


def test_deterministic_fallback_has_only_verified_facts() -> None:
    text = _fallback_text(_result())
    assert "Alpha" in text and "available" in text
    assert "골든리트리버" not in text
    unavailable = _fallback_text(_result(available=False))
    assert unavailable == "조회 결과 1건"


def test_answer_is_never_composed_for_unavailable_execution(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from app.services.agent_run_answer_composer import AgentRunAnswerComposer
    from app.services.agent_run_chat_result import AgentRunChatResultService

    async def fake_result(*, agent_request_id, requester_id):
        return _result(available=False)

    fake_get = AsyncMock(side_effect=fake_result)
    monkeypatch.setattr(AgentRunChatResultService, "get_for_owner", fake_get)
    composer = AgentRunAnswerComposer(session=object())
    response = asyncio.run(
        composer.compose_for_owner(
            agent_request_id=uuid.uuid4(), requester_id=uuid.uuid4(),
        )
    )
    assert response.generated is False
    assert response.fallback_reason == "SCHEMA_INVALID"
    assert fake_get.await_count == 1
