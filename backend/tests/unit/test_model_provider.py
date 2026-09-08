"""Unit tests for OpenAI-compatible base_url helpers and secret re-exports."""

from __future__ import annotations

from app.mcp import secrets as mcp_secrets
from app.model_provider.base_url import join_api_path, normalize_openai_compatible_root


def test_normalize_openai_compatible_root_variants() -> None:
    assert normalize_openai_compatible_root("https://host") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1/") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/openai/") == "https://host/openai/v1"
    joined = join_api_path(
        normalize_openai_compatible_root("https://host/v1/"), "chat/completions"
    )
    assert joined == "https://host/v1/chat/completions"
    assert "/v1/v1" not in joined


def test_mcp_secrets_reexports_core() -> None:
    from app.core import secrets as core_secrets

    assert mcp_secrets.ResolvedSecret is core_secrets.ResolvedSecret
    assert mcp_secrets.UnimplementedSecretResolver is core_secrets.UnimplementedSecretResolver
    resolver = mcp_secrets.UnimplementedSecretResolver()
    assert resolver is not None
