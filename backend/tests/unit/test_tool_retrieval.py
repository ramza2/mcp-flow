"""Unit tests for authorized hybrid Tool retrieval helpers."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.errors import AppError
from app.domain.enums import RiskClass
from app.model_provider.errors import PROTOCOL, ModelProviderError
from app.repositories.tool_retrieval import (
    RRF_K,
    ToolRetrievalHit,
    normalize_rrf_score,
    rrf_contribution,
)
from app.search.tool_retrieval import (
    ToolRetrievalService,
    build_candidate_description,
    build_tool_candidate_descriptor,
    map_retrieval_hit,
    split_input_fields,
    summarize_output_schema,
)


def _hit(**overrides: object) -> ToolRetrievalHit:
    base = dict(
        mcp_tool_id=uuid.uuid4(),
        tool_version_id=uuid.uuid4(),
        remote_name="weather_lookup",
        description_override=None,
        remote_description="lookup weather",
        tags=["Weather", "weather"],
        input_schema={
            "properties": {"location": {}, "date": {}},
            "required": ["location"],
        },
        output_schema={"description": "forecast"},
        lexical_rank=1,
        vector_rank=None,
        rrf_raw=1 / 61,
        retrieval_score=normalize_rrf_score(1 / 61),
        agent_requires_confirmation=True,
        parameter_constraints={"location": {"type": "string"}},
        policy_present=True,
        tool_policy_id=uuid.uuid4(),
        tool_policy_lock_version=3,
        risk_class=RiskClass.READ_ONLY.value,
        policy_requires_confirmation=True,
        policy_requires_approval=True,
        approval_policy_id=uuid.uuid4(),
        allow_auto_select=False,
    )
    base.update(overrides)
    return ToolRetrievalHit(**base)  # type: ignore[arg-type]


def test_rrf_contribution_and_normalization() -> None:
    raw_a = rrf_contribution(1) + rrf_contribution(1)
    assert abs(raw_a - (2 / (RRF_K + 1))) < 1e-12
    assert abs(normalize_rrf_score(raw_a) - 1.0) < 1e-12

    raw_b = rrf_contribution(2)
    score_b = normalize_rrf_score(raw_b)
    assert score_b < 0.5
    assert score_b == pytest.approx((1 / 62) / (2 / 61))

    raw_c = rrf_contribution(None) + rrf_contribution(2)
    assert abs(raw_c - raw_b) < 1e-12
    assert normalize_rrf_score(raw_a) > score_b


def test_rrf_source_agreement_beats_single_source() -> None:
    both_mid = rrf_contribution(3) + rrf_contribution(3)
    lexical_only_top = rrf_contribution(1)
    assert both_mid > lexical_only_top


def test_rrf_tie_break_ordering_key() -> None:
    id_a = uuid.UUID("00000000-0000-0000-0000-000000000002")
    id_b = uuid.UUID("00000000-0000-0000-0000-000000000001")
    raw = rrf_contribution(5)
    ranked = sorted(
        [(raw, id_a), (raw, id_b)],
        key=lambda item: (-item[0], item[1]),
    )
    assert ranked[0][1] == id_b


def test_descriptor_description_override_and_remote_fallback() -> None:
    assert (
        build_candidate_description(
            description_override="  Override  text ",
            remote_description="Remote",
        )
        == "Override text"
    )
    assert (
        build_candidate_description(
            description_override="   ",
            remote_description="  Remote  desc ",
        )
        == "Remote desc"
    )
    assert (
        build_candidate_description(
            description_override=None,
            remote_description=None,
        )
        is None
    )


def test_split_input_fields_and_malformed_schema() -> None:
    required, optional = split_input_fields(
        {
            "type": "object",
            "properties": {"b": {}, "a": {}, "c": {}},
            "required": ["c", "missing"],
        }
    )
    assert required == ["c"]
    assert optional == ["a", "b"]
    assert split_input_fields(None) == ([], [])
    assert split_input_fields("bad") == ([], [])
    assert split_input_fields({"properties": "nope"}) == ([], [])


def test_output_summary_description_and_property_fallback() -> None:
    assert (
        summarize_output_schema({"description": "  condition, precip  "})
        == "condition, precip"
    )
    assert summarize_output_schema({"properties": {"z": {}, "a": {}}}) == "a, z"
    assert summarize_output_schema({"properties": []}) is None
    assert summarize_output_schema(None) is None


def test_descriptor_excludes_secrets_and_maps_risk() -> None:
    hit = _hit()
    desc = build_tool_candidate_descriptor(hit)
    assert desc.name == "weather_lookup"
    assert desc.tags == ("weather",)
    assert desc.required_inputs == ("location",)
    assert desc.optional_inputs == ("date",)
    assert desc.risk_class == RiskClass.READ_ONLY.value
    fields = set(desc.__dataclass_fields__)
    for forbidden in (
        "endpoint_url",
        "auth_secret_id",
        "credential_secret_id",
        "raw_descriptor",
        "input_schema",
        "output_schema",
        "annotations",
    ):
        assert forbidden not in fields

    mapped = map_retrieval_hit(hit)
    assert mapped.policy_present is True
    assert mapped.allow_auto_select is False
    assert mapped.agent_requires_confirmation is True
    assert mapped.policy_requires_confirmation is True


def test_missing_policy_risk_defaults_unknown_on_hit() -> None:
    hit = _hit(
        policy_present=False,
        tool_policy_id=None,
        tool_policy_lock_version=None,
        risk_class=RiskClass.UNKNOWN.value,
        policy_requires_confirmation=None,
        policy_requires_approval=None,
        approval_policy_id=None,
        allow_auto_select=None,
        lexical_rank=1,
        vector_rank=1,
        rrf_raw=2 / 61,
        retrieval_score=1.0,
    )
    desc = build_tool_candidate_descriptor(hit)
    assert desc.risk_class == RiskClass.UNKNOWN.value
    assert map_retrieval_hit(hit).allow_auto_select is None


@pytest.mark.asyncio
async def test_blank_query_rejected_without_provider_call() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    provider = MagicMock()
    provider.embed_texts = AsyncMock(
        side_effect=AssertionError("provider must not be called")
    )
    service = ToolRetrievalService(session, model_provider=provider)
    with pytest.raises(AppError) as exc:
        await service.retrieve(
            user_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            query_text="   \n\t  ",
        )
    assert exc.value.code == "VALIDATION_ERROR"
    provider.embed_texts.assert_not_called()


@pytest.mark.asyncio
async def test_missing_agent_version_not_found() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    service = ToolRetrievalService(session, model_provider=MagicMock())
    service._agent_versions.get = AsyncMock(return_value=None)
    with pytest.raises(AppError) as exc:
        await service.retrieve(
            user_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            query_text="weather",
        )
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_profile_race_retries_once_then_conflict() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    provider = MagicMock()
    provider.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])

    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.lock_version = 1
    profile.provider = "OPENAI_COMPATIBLE"
    profile.model = "emb"
    profile.base_url = "https://llm.test/v1"
    profile.dimension = 4
    profile.credential_secret_id = None

    service = ToolRetrievalService(session, model_provider=provider)
    service._agent_versions.get = AsyncMock(return_value=MagicMock())
    service._profiles.get_active_for_tools = AsyncMock(return_value=profile)

    stale = MagicMock()
    stale.profile_current = False
    stale.hits = []
    service._retrieval.authorized_hybrid_search = AsyncMock(return_value=stale)

    with pytest.raises(AppError) as exc:
        await service.retrieve(
            user_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            query_text="weather in seoul",
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    assert service._retrieval.authorized_hybrid_search.await_count == 2
    assert provider.embed_texts.await_count == 2


@pytest.mark.asyncio
async def test_owned_provider_client_closed_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.commit = AsyncMock()

    owned = MagicMock()
    owned.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    owned.aclose = AsyncMock()
    monkeypatch.setattr(
        "app.search.tool_retrieval.ModelProviderClient",
        lambda: owned,
    )

    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.lock_version = 1
    profile.provider = "OPENAI_COMPATIBLE"
    profile.model = "emb"
    profile.base_url = "https://llm.test/v1"
    profile.dimension = 4
    profile.credential_secret_id = None

    service = ToolRetrievalService(session)
    service._agent_versions.get = AsyncMock(return_value=MagicMock())
    service._profiles.get_active_for_tools = AsyncMock(return_value=profile)
    ok = MagicMock()
    ok.profile_current = True
    ok.hits = []
    service._retrieval.authorized_hybrid_search = AsyncMock(return_value=ok)

    result = await service.retrieve(
        user_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        query_text="weather",
    )
    assert result.candidates == ()
    owned.embed_texts.assert_awaited_once()
    owned.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_provider_client_closed_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.commit = AsyncMock()

    owned = MagicMock()
    owned.embed_texts = AsyncMock(
        side_effect=ModelProviderError(
            error_code=PROTOCOL,
            message="boom",
            retryable=False,
        )
    )
    owned.aclose = AsyncMock()
    monkeypatch.setattr(
        "app.search.tool_retrieval.ModelProviderClient",
        lambda: owned,
    )

    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.lock_version = 1
    profile.provider = "OPENAI_COMPATIBLE"
    profile.model = "emb"
    profile.base_url = "https://llm.test/v1"
    profile.dimension = 4
    profile.credential_secret_id = None

    service = ToolRetrievalService(session)
    service._agent_versions.get = AsyncMock(return_value=MagicMock())
    service._profiles.get_active_for_tools = AsyncMock(return_value=profile)

    with pytest.raises(ModelProviderError):
        await service.retrieve(
            user_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            query_text="weather",
        )
    owned.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_injected_provider_client_not_closed() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    provider = MagicMock()
    provider.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    provider.aclose = AsyncMock()

    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.lock_version = 1
    profile.provider = "OPENAI_COMPATIBLE"
    profile.model = "emb"
    profile.base_url = "https://llm.test/v1"
    profile.dimension = 4
    profile.credential_secret_id = None

    service = ToolRetrievalService(session, model_provider=provider)
    service._agent_versions.get = AsyncMock(return_value=MagicMock())
    service._profiles.get_active_for_tools = AsyncMock(return_value=profile)
    ok = MagicMock()
    ok.profile_current = True
    ok.hits = []
    service._retrieval.authorized_hybrid_search = AsyncMock(return_value=ok)

    await service.retrieve(
        user_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        query_text="weather",
    )
    provider.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_profile_retry_closes_owned_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.commit = AsyncMock()

    owned = MagicMock()
    owned.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    owned.aclose = AsyncMock()
    monkeypatch.setattr(
        "app.search.tool_retrieval.ModelProviderClient",
        lambda: owned,
    )

    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.lock_version = 1
    profile.provider = "OPENAI_COMPATIBLE"
    profile.model = "emb"
    profile.base_url = "https://llm.test/v1"
    profile.dimension = 4
    profile.credential_secret_id = None

    service = ToolRetrievalService(session)
    service._agent_versions.get = AsyncMock(return_value=MagicMock())
    service._profiles.get_active_for_tools = AsyncMock(return_value=profile)

    stale = MagicMock()
    stale.profile_current = False
    stale.hits = []
    service._retrieval.authorized_hybrid_search = AsyncMock(return_value=stale)

    with pytest.raises(AppError) as exc:
        await service.retrieve(
            user_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            query_text="weather",
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    assert owned.embed_texts.await_count == 2
    owned.aclose.assert_awaited_once()


def test_repository_rejects_non_positive_limits() -> None:
    from app.repositories.tool_retrieval import _validate_limits

    with pytest.raises(ValueError):
        _validate_limits(lexical_limit=0, vector_limit=40, merged_limit=20, rrf_k=60)
    with pytest.raises(ValueError):
        _validate_limits(lexical_limit=40, vector_limit=40, merged_limit=20, rrf_k=0)
