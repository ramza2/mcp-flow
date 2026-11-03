"""Focused redaction tests for requester-scoped Tool Test response previews."""

from app.audit.sanitize import REDACTION_MARKER
from app.services.manual_tool_test_result import _safe_text_items


def test_echo_text_is_visible_without_metadata_or_structured_payload() -> None:
    content = {
        "content": [
            {
                "type": "text",
                "text": '{"message":"MCPFlow echo E2E 테스트 성공"}',
            }
        ],
        "structured_content": {"password": "do-not-leak"},
        "metadata": {"authorization": "do-not-leak"},
    }
    texts, truncated = _safe_text_items(content)
    assert texts == ['{"message":"MCPFlow echo E2E 테스트 성공"}']
    assert truncated is False
    assert "do-not-leak" not in str(texts)


def test_credential_bearing_text_is_fail_closed() -> None:
    for label in ("password", "API_KEY", "Bearer", "requestState", "authorization"):
        texts, truncated = _safe_text_items(
            {"content": [{"type": "text", "text": f"{label}=secret123"}]}
        )
        assert texts == [REDACTION_MARKER]
        assert truncated is False


def test_binary_and_oversized_responses_are_not_exposed() -> None:
    no_text, _ = _safe_text_items(
        {"content": [{"type": "image", "data": "hidden"}]}
    )
    assert no_text == []
    large, too_large = _safe_text_items(
        {"content": [
            {"type": "text", "text": "가" * 999},
            {"type": "text", "text": "가" * 999},
            {"type": "text", "text": "가" * 999},
            {"type": "text", "text": "가" * 999},
        ]}
    )
    assert large == []
    assert too_large is True


def test_response_text_item_count_is_bounded() -> None:
    texts, truncated = _safe_text_items(
        {"content": [{"type": "text", "text": "ping"} for _ in range(10)]}
    )
    assert texts == ["ping"] * 4
    assert truncated is True
