"""Alembic round-trip for tool selection + clarification tables."""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


@pytest.mark.integration
def test_alembic_tool_selection_clarification_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260911_0008")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_selection_clarification_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        tables = (
            await session.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                        'tool_selection_runs',
                        'tool_selection_candidates',
                        'clarification_requests'
                      )
                    ORDER BY table_name
                    """
                )
            )
        ).scalars().all()
        assert tables == [
            "clarification_requests",
            "tool_selection_candidates",
            "tool_selection_runs",
        ]
        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_runs'::regclass
                      AND contype = 'c'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert any("decision" in name for name in checks)
        assert "ck_tool_selection_runs_selected_tool_consistency" in checks
        candidate_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_candidates'::regclass
                      AND contype = 'c'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert "ck_tool_selection_candidates_risk_class" in candidate_checks
        uniques = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_candidates'::regclass
                      AND contype = 'u'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert uniques
        fks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_runs'::regclass
                      AND contype = 'f'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert any("selected_tool_version" in name for name in fks)

@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_selected_tool_consistency_check(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NO_MATCH + selected OR AUTO_SELECT + null selected must fail DB CHECK."""
    import uuid

    from sqlalchemy.exc import IntegrityError

    from app.agent.tool_selector import ToolSelectorService
    from app.domain.enums import AgentToolGrantEffect, RiskClass
    from app.repositories.agent import AgentRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.repositories.tool_selection import ToolSelectionRepository
    from tests.integration.test_tool_selector import (
        _create_active_profile,
        _provider_client,
        _seed_active_tool,
        _seed_agent_version,
        _seed_authorized_user,
        _seed_embedding,
        _seed_retrieving_request,
    )

    query = "check consistency weather UNIQUE_CK"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session, remote_name="ck_weather", description=query
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool])
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver),
                    "llm_fit_score": 0.3,
                    "reason_summary": "weak",
                }
            ],
            "ambiguities": [],
        },
    )
    async with integration_session_factory() as session:
        outcome = await ToolSelectorService(
            session, model_provider=provider
        ).select(agent_request_id=request_id)
        assert outcome.decision == "NO_MATCH"
        run = await ToolSelectionRepository(session).get_latest_for_agent_request(
            request_id
        )
        assert run is not None
        assert run.selected_tool_version_id is None

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_selection_runs (
                        id, agent_request_id, agent_version_id,
                        embedding_profile_id, llm_profile_id,
                        registry_snapshot, model_snapshot, threshold_snapshot,
                        decision, selected_tool_version_id, ambiguities
                    ) VALUES (
                        :id, :agent_request_id, :agent_version_id,
                        :embedding_profile_id, :llm_profile_id,
                        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                        'NO_MATCH', :selected_tool_version_id, '[]'::jsonb
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "agent_request_id": run.agent_request_id,
                    "agent_version_id": run.agent_version_id,
                    "embedding_profile_id": run.embedding_profile_id,
                    "llm_profile_id": run.llm_profile_id,
                    "selected_tool_version_id": ver,
                },
            )
            await session.flush()
        await session.rollback()

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_selection_runs (
                        id, agent_request_id, agent_version_id,
                        embedding_profile_id, llm_profile_id,
                        registry_snapshot, model_snapshot, threshold_snapshot,
                        decision, selected_tool_version_id, ambiguities
                    ) VALUES (
                        :id, :agent_request_id, :agent_version_id,
                        :embedding_profile_id, :llm_profile_id,
                        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                        'AUTO_SELECT', NULL, '[]'::jsonb
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "agent_request_id": run.agent_request_id,
                    "agent_version_id": run.agent_version_id,
                    "embedding_profile_id": run.embedding_profile_id,
                    "llm_profile_id": run.llm_profile_id,
                },
            )
            await session.flush()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_candidate_risk_class_check(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """risk_class='WRITE' fails; canonical RiskClass succeeds."""
    import uuid

    from sqlalchemy.exc import IntegrityError

    from app.agent.tool_selector import ToolSelectorService
    from app.domain.enums import AgentToolGrantEffect, RiskClass
    from app.repositories.agent import AgentRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.repositories.tool_selection import ToolSelectionRepository
    from tests.integration.test_tool_selector import (
        _create_active_profile,
        _provider_client,
        _seed_active_tool,
        _seed_agent_version,
        _seed_authorized_user,
        _seed_embedding,
        _seed_retrieving_request,
    )

    query = "risk class check weather UNIQUE_RISK"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session, remote_name="risk_weather", description=query
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool])
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver),
                    "llm_fit_score": 0.3,
                    "reason_summary": "weak",
                }
            ],
            "ambiguities": [],
        },
    )
    async with integration_session_factory() as session:
        await ToolSelectorService(session, model_provider=provider).select(
            agent_request_id=request_id
        )
        run = await ToolSelectionRepository(session).get_latest_for_agent_request(
            request_id
        )
        assert run is not None

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_selection_candidates (
                        id, tool_selection_run_id, tool_version_id,
                        input_rank, retrieval_score, llm_fit_score,
                        reason_summary, risk_class
                    ) VALUES (
                        :id, :run_id, :tool_version_id,
                        99, 0.5, 0.5, 'bad risk', 'WRITE'
                    )
                    """
                ),
                {"id": uuid.uuid4(), "run_id": run.id, "tool_version_id": ver},
            )
            await session.flush()
        await session.rollback()

        await session.execute(
            text(
                """
                INSERT INTO tool_selection_candidates (
                    id, tool_selection_run_id, tool_version_id,
                    input_rank, retrieval_score, llm_fit_score,
                    reason_summary, risk_class
                ) VALUES (
                    :id, :run_id, :tool_version_id,
                    99, 0.5, 0.5, 'ok risk', 'READ_ONLY'
                )
                """
            ),
            {"id": uuid.uuid4(), "run_id": run.id, "tool_version_id": ver},
        )
        await session.commit()
