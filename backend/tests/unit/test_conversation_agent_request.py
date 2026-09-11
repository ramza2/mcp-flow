"""Unit tests for Conversation / AgentRequest persistence foundation."""

from __future__ import annotations

import uuid

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ConversationMessageRole,
    ConversationMessageVisibility,
    ConversationStatus,
)
from app.models.agent import Agent, AgentVersion
from app.models.auth import User
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.conversation_message import ConversationMessageRepository
from app.repositories.user import UserRepository
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_owner_agent_version(
    session: AsyncSession,
) -> tuple[User, Agent, AgentVersion]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Conv Agent",
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
    await session.flush()
    return user, agent, version


@pytest.mark.asyncio
async def test_create_conversation_and_append_message_orders_by_sequence(
    db_session: AsyncSession,
) -> None:
    user, agent, _version = await _seed_owner_agent_version(db_session)
    svc = ConversationService(db_session)

    conversation = await svc.create_conversation(
        owner_id=user.id,
        agent_id=agent.id,
        title="Hello",
    )
    assert conversation.status == ConversationStatus.ACTIVE.value
    assert conversation.lock_version == 1
    assert conversation.last_message_at is None

    lock_before = conversation.lock_version
    m1 = await svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role=ConversationMessageRole.USER,
        content={"text": "one"},
        content_text="one",
    )
    m2 = await svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role=ConversationMessageRole.ASSISTANT,
        content={"text": "two"},
        content_text="two",
        visibility=ConversationMessageVisibility.USER,
    )
    await db_session.refresh(conversation)

    assert m1.sequence_no == 1
    assert m2.sequence_no == 2
    assert conversation.last_message_at == m2.created_at
    # Message append must not bump optimistic lock used by metadata CAS.
    assert conversation.lock_version == lock_before

    listed = await ConversationMessageRepository(db_session).list_for_conversation(
        conversation.id
    )
    assert [m.sequence_no for m in listed] == [1, 2]
    assert [m.content_text for m in listed] == ["one", "two"]


@pytest.mark.asyncio
async def test_metadata_cas_still_works_after_append(
    db_session: AsyncSession,
) -> None:
    user, agent, _version = await _seed_owner_agent_version(db_session)
    svc = ConversationService(db_session)
    conversation = await svc.create_conversation(
        owner_id=user.id, agent_id=agent.id, title="T"
    )
    await svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role="USER",
        content={"text": "hi"},
        content_text="hi",
    )
    await db_session.refresh(conversation)
    lock_before = conversation.lock_version
    updated = await svc.update_metadata(
        conversation_id=conversation.id,
        owner_id=user.id,
        expected_lock_version=lock_before,
        title="Renamed",
        status=ConversationStatus.ARCHIVED,
    )
    assert updated.title == "Renamed"
    assert updated.status == ConversationStatus.ARCHIVED.value
    assert updated.lock_version == lock_before + 1

    with pytest.raises(AppError) as exc:
        await svc.update_metadata(
            conversation_id=conversation.id,
            owner_id=user.id,
            expected_lock_version=lock_before,
            title="stale",
        )
    assert exc.value.code == "RESOURCE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_agent_request_received_snapshots_raw_text(
    db_session: AsyncSession,
) -> None:
    user, agent, version = await _seed_owner_agent_version(db_session)
    conv_svc = ConversationService(db_session)
    req_svc = AgentRequestService(db_session)

    conversation = await conv_svc.create_conversation(
        owner_id=user.id, agent_id=agent.id, title="Req"
    )
    message = await conv_svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role=ConversationMessageRole.USER,
        content={"text": "original request"},
        content_text="original request",
    )

    request = await req_svc.create_received(
        conversation_id=conversation.id,
        requester_id=user.id,
        agent_version_id=version.id,
        source_message_id=message.id,
        trace_id="trace-abc",
    )
    assert request.status == AgentRequestStatus.RECEIVED.value
    assert request.raw_request_text == "original request"
    assert request.structured_request is None
    assert request.structured_request_version is None
    assert request.missing_fields == []
    assert request.rejection_code is None
    assert request.analyzed_at is None
    assert request.completed_at is None
    assert request.trace_id == "trace-abc"

    # Fixture-level mutation of source message must not change snapshot.
    await db_session.execute(
        text(
            "UPDATE conversation_messages SET content_text = :t WHERE id = :id"
        ),
        {"t": "mutated later", "id": str(message.id)},
    )
    await db_session.flush()
    refreshed = await AgentRequestRepository(db_session).get(request.id)
    assert refreshed is not None
    assert refreshed.raw_request_text == "original request"


@pytest.mark.asyncio
async def test_agent_request_rejects_cross_conversation_and_non_user(
    db_session: AsyncSession,
) -> None:
    user, agent, version = await _seed_owner_agent_version(db_session)
    other_user, other_agent, other_version = await _seed_owner_agent_version(db_session)
    conv_svc = ConversationService(db_session)
    req_svc = AgentRequestService(db_session)

    conversation = await conv_svc.create_conversation(
        owner_id=user.id, agent_id=agent.id, title="A"
    )
    other_conversation = await conv_svc.create_conversation(
        owner_id=other_user.id, agent_id=other_agent.id, title="B"
    )
    user_msg = await conv_svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role="USER",
        content={"text": "a"},
        content_text="a",
    )
    foreign_msg = await conv_svc.append_message(
        conversation_id=other_conversation.id,
        owner_id=other_user.id,
        role="USER",
        content={"text": "b"},
        content_text="b",
    )
    assistant_msg = await conv_svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role="ASSISTANT",
        content={"text": "reply"},
        content_text="reply",
    )

    with pytest.raises(AppError) as cross:
        await req_svc.create_received(
            conversation_id=conversation.id,
            requester_id=user.id,
            agent_version_id=version.id,
            source_message_id=foreign_msg.id,
        )
    assert cross.value.code == "VALIDATION_ERROR"

    with pytest.raises(AppError) as non_user:
        await req_svc.create_received(
            conversation_id=conversation.id,
            requester_id=user.id,
            agent_version_id=version.id,
            source_message_id=assistant_msg.id,
        )
    assert non_user.value.code == "VALIDATION_ERROR"

    with pytest.raises(AppError) as owner_mismatch:
        await req_svc.create_received(
            conversation_id=conversation.id,
            requester_id=other_user.id,
            agent_version_id=version.id,
            source_message_id=user_msg.id,
        )
    assert owner_mismatch.value.code == "FORBIDDEN"

    with pytest.raises(AppError) as version_mismatch:
        await req_svc.create_received(
            conversation_id=conversation.id,
            requester_id=user.id,
            agent_version_id=other_version.id,
            source_message_id=user_msg.id,
        )
    assert version_mismatch.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_agent_request_cas_success_and_stale_failure(
    db_session: AsyncSession,
) -> None:
    user, agent, version = await _seed_owner_agent_version(db_session)
    conv_svc = ConversationService(db_session)
    req_svc = AgentRequestService(db_session)
    conversation = await conv_svc.create_conversation(
        owner_id=user.id, agent_id=agent.id, title="CAS"
    )
    message = await conv_svc.append_message(
        conversation_id=conversation.id,
        owner_id=user.id,
        role="USER",
        content={"text": "go"},
        content_text="go",
    )
    request = await req_svc.create_received(
        conversation_id=conversation.id,
        requester_id=user.id,
        agent_version_id=version.id,
        source_message_id=message.id,
    )

    updated = await req_svc.compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RECEIVED],
        new_status=AgentRequestStatus.ANALYZING,
    )
    assert updated.status == AgentRequestStatus.ANALYZING.value
    assert updated.completed_at is None

    with pytest.raises(AppError) as stale:
        await req_svc.compare_and_set_status(
            request.id,
            expected_statuses=[AgentRequestStatus.RECEIVED],
            new_status=AgentRequestStatus.CANCELLED,
            set_completed_at=True,
        )
    assert stale.value.code == "RESOURCE_CONFLICT"

    terminal = await req_svc.compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.ANALYZING],
        new_status=AgentRequestStatus.READY,
        set_completed_at=True,
    )
    assert terminal.status == AgentRequestStatus.READY.value
    assert terminal.completed_at is not None


@pytest.mark.asyncio
async def test_message_repository_has_no_update_or_delete_api() -> None:
    methods = dir(ConversationMessageRepository)
    assert "update" not in methods
    assert "delete" not in methods
    assert "append" in methods
