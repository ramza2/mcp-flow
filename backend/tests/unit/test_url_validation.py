"""Unit tests for MCP endpoint URL validation."""

from __future__ import annotations

import pytest
from app.core.errors import AppError
from app.core.url_validation import validate_mcp_endpoint_url


def test_accepts_http_and_https() -> None:
    assert validate_mcp_endpoint_url("http://mcp.example/mcp") == "http://mcp.example/mcp"
    assert validate_mcp_endpoint_url("https://mcp.example/mcp") == "https://mcp.example/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://mcp.example/mcp",
        "ws://mcp.example/mcp",
        "file:///etc/passwd",
    ],
)
def test_rejects_empty_or_non_http_schemes(url: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_mcp_endpoint_url(url)
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "https://user@mcp.example/mcp",
        "https://user:pass@mcp.example/mcp",
        "http://admin:secret@localhost/mcp",
    ],
)
def test_rejects_userinfo(url: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_mcp_endpoint_url(url)
    assert exc.value.code == "VALIDATION_ERROR"
    assert "userinfo" in exc.value.message.lower()


@pytest.mark.parametrize(
    "url",
    [
        "https:///mcp",
        "http:///path",
    ],
)
def test_rejects_missing_hostname(url: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_mcp_endpoint_url(url)
    assert exc.value.code == "VALIDATION_ERROR"
    assert "hostname" in exc.value.message.lower()


def test_rejects_excessive_length() -> None:
    long_host = "a" * 2050
    url = f"https://{long_host}/mcp"
    with pytest.raises(AppError) as exc:
        validate_mcp_endpoint_url(url)
    assert exc.value.code == "VALIDATION_ERROR"
    assert "length" in exc.value.message.lower()


def test_strips_surrounding_whitespace() -> None:
    assert (
        validate_mcp_endpoint_url("  https://mcp.example/mcp  ")
        == "https://mcp.example/mcp"
    )
