"""PostgreSQL integration tests for Conversation / AgentRequest persistence."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.core.errors import AppError
from app.domain.enums import AgentRequestStatus, ConversationMessageRole
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.conversation_message import ConversationMessageRepository
from app.repositories.user import UserRepository
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


async def _seed(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Integration Agent",
        owner_id=user.id,
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="plan safely",
        llm_profile_id=uuid.uuid4(),
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={},
        planning_settings={},
        response_settings={},
        content_hash=uuid.uuid4().hex,
    )
    await session.commit()
    return user.id, agent.id, version.id


@pytest.mark.integration
def test_alembic_conversation_agent_requests_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260908_0007")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_schema_fk_check_unique_and_circular_fk(
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
                        'conversations',
                        'conversation_messages',
                        'agent_requests'
                      )
                    ORDER BY table_name
                    """
                )
            )
        ).scalars().all()
        assert tables == [
            "agent_requests",
            "conversation_messages",
            "conversations",
        ]

        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE contype = 'c'
                      AND conrelid::regclass::text IN (
                        'conversations',
                        'conversation_messages',
                        'agent_requests'
                      )
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        # Naming convention prefixes ck_<table>_ onto the CheckConstraint name.
        assert "ck_conversations_ck_conversations_status" in checks
        assert "ck_conversation_messages_ck_conversation_messages_role" in checks
        assert (
            "ck_conversation_messages_ck_conversation_messages_visibility" in checks
        )
        assert "ck_agent_requests_ck_agent_requests_status" in checks

        fks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid::regclass::text IN (
                        'conversations',
                        'conversation_messages',
                        'agent_requests'
                      )
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert "fk_conversation_messages_agent_request_id_agent_requests" in fks
        assert "fk_agent_requests_source_message_id_conversation_messages" in fks
        # execution_id must NOT have a FK (executions table does not exist yet)
        assert not any("execution_id" in name for name in fks)

        uniques = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE contype = 'u'
                      AND conrelid::regclass::text = 'conversation_messages'
                    """
                )
            )
        ).scalars().all()
        assert "uq_conversation_messages_conversation_sequence_no" in uniques

        user_id, agent_id, _version_id = await _seed(session)
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO conversations (
                      id, owner_id, agent_id, title, status, lock_version
                    ) VALUES (
                      :id, :owner_id, :agent_id, 'bad', 'CLOSED', 1
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "owner_id": str(user_id),
                    "agent_id": str(agent_id),
                },
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_message_append_allocates_contiguous_sequences(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_id, agent_id, _version_id = await _seed(session)
        conversation = await ConversationService(session).create_conversation(
            owner_id=user_id,
            agent_id=agent_id,
            title="Concurrent",
        )
        await session.commit()
        conversation_id = conversation.id

    async def _append(i: int) -> uuid.UUID:
        async with integration_session_factory() as session:
            msg = await ConversationService(session).append_message(
                conversation_id=conversation_id,
                owner_id=user_id,
                role=ConversationMessageRole.USER,
                content={"text": f"m-{i}"},
                content_text=f"m-{i}",
            )
            await session.commit()
            return msg.id

    ids = await asyncio.gather(*[_append(i) for i in range(10)])
    assert len(ids) == 10
    assert len(set(ids)) == 10

    async with integration_session_factory() as session:
        messages = await ConversationMessageRepository(session).list_for_conversation(
            conversation_id
        )
        sequences = [m.sequence_no for m in messages]
        assert sequences == list(range(1, 11))
        conversation = await ConversationRepository(session).get(conversation_id)
        assert conversation is not None
        assert conversation.last_message_at == messages[-1].created_at
        assert conversation.lock_version == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_request_cas_concurrency_single_winner(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_id, agent_id, version_id = await _seed(session)
        conv_svc = ConversationService(session)
        conversation = await conv_svc.create_conversation(
            owner_id=user_id, agent_id=agent_id, title="CAS"
        )
        message = await conv_svc.append_message(
            conversation_id=conversation.id,
            owner_id=user_id,
            role="USER",
            content={"text": "go"},
            content_text="go",
        )
        request = await AgentRequestService(session).create_received(
            conversation_id=conversation.id,
            requester_id=user_id,
            agent_version_id=version_id,
            source_message_id=message.id,
        )
        await session.commit()
        request_id = request.id

    results: list[str] = []

    async def _transition(new_status: AgentRequestStatus) -> None:
        async with integration_session_factory() as session:
            try:
                await AgentRequestService(session).compare_and_set_status(
                    request_id,
                    expected_statuses=[AgentRequestStatus.RECEIVED],
                    new_status=new_status,
                    set_completed_at=(new_status == AgentRequestStatus.CANCELLED),
                )
                await session.commit()
                results.append(f"ok:{new_status.value}")
            except AppError as exc:
                await session.rollback()
                results.append(f"err:{exc.code}")

    await asyncio.gather(
        _transition(AgentRequestStatus.ANALYZING),
        _transition(AgentRequestStatus.CANCELLED),
    )

    assert sum(1 for r in results if r.startswith("ok:")) == 1
    assert sum(1 for r in results if r.startswith("err:")) == 1

    async with integration_session_factory() as session:
        final = await AgentRequestRepository(session).get(request_id)
        assert final is not None
        assert final.status in {
            AgentRequestStatus.ANALYZING.value,
            AgentRequestStatus.CANCELLED.value,
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_historical_fk_restricts_physical_delete(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_id, agent_id, version_id = await _seed(session)
        conv_svc = ConversationService(session)
        conversation = await conv_svc.create_conversation(
            owner_id=user_id, agent_id=agent_id, title="Hist"
        )
        message = await conv_svc.append_message(
            conversation_id=conversation.id,
            owner_id=user_id,
            role="USER",
            content={"text": "keep"},
            content_text="keep",
        )
        request = await AgentRequestService(session).create_received(
            conversation_id=conversation.id,
            requester_id=user_id,
            agent_version_id=version_id,
            source_message_id=message.id,
        )
        await session.commit()
        request_id = request.id
        version_id_s = str(version_id)
        conversation_id_s = str(conversation.id)
        message_id_s = str(message.id)

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM agent_versions WHERE id = :id"),
                {"id": version_id_s},
            )
            await session.commit()
        await session.rollback()

        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM conversation_messages WHERE id = :id"),
                {"id": message_id_s},
            )
            await session.commit()
        await session.rollback()

        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM conversations WHERE id = :id"),
                {"id": conversation_id_s},
            )
            await session.commit()
        await session.rollback()

        still = await AgentRequestRepository(session).get(request_id)
        assert still is not None
        assert still.status == AgentRequestStatus.RECEIVED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_request_text_snapshot_immutable(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_id, agent_id, version_id = await _seed(session)
        conv_svc = ConversationService(session)
        conversation = await conv_svc.create_conversation(
            owner_id=user_id, agent_id=agent_id, title="Snap"
        )
        message = await conv_svc.append_message(
            conversation_id=conversation.id,
            owner_id=user_id,
            role="USER",
            content={"text": "snapshot me"},
            content_text="snapshot me",
        )
        request = await AgentRequestService(session).create_received(
            conversation_id=conversation.id,
            requester_id=user_id,
            agent_version_id=version_id,
            source_message_id=message.id,
        )
        await session.commit()
        request_id = request.id
        message_id = message.id

    async with integration_session_factory() as session:
        await session.execute(
            text(
                "UPDATE conversation_messages SET content_text = :t WHERE id = :id"
            ),
            {"t": "mutated", "id": str(message_id)},
        )
        await session.commit()

    async with integration_session_factory() as session:
        refreshed = await AgentRequestRepository(session).get(request_id)
        assert refreshed is not None
        assert refreshed.raw_request_text == "snapshot me"
