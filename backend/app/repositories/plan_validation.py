"""Plan validation run repository — docs/05 §10.8."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan_validation import PlanValidationRun
from app.repositories.plan_generation import validate_plan_hash


class PlanValidationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        agent_request_id: uuid.UUID,
        plan_generation_run_id: uuid.UUID,
        plan_hash: str,
        validator_version: str,
        decision: str,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        checks_snapshot: dict[str, Any],
        policy_snapshot: dict[str, Any],
        confirmation_required: bool,
        clarification_request_id: uuid.UUID | None,
    ) -> PlanValidationRun:
        validate_plan_hash(plan_hash)
        row = PlanValidationRun(
            id=uuid.uuid4(),
            agent_request_id=agent_request_id,
            plan_generation_run_id=plan_generation_run_id,
            plan_hash=plan_hash,
            validator_version=validator_version,
            decision=decision,
            errors=list(errors),
            warnings=list(warnings),
            checks_snapshot=dict(checks_snapshot),
            policy_snapshot=dict(policy_snapshot),
            confirmation_required=confirmation_required,
            clarification_request_id=clarification_request_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(self, run_id: uuid.UUID) -> PlanValidationRun | None:
        stmt = select(PlanValidationRun).where(PlanValidationRun.id == run_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> PlanValidationRun | None:
        stmt = (
            select(PlanValidationRun)
            .where(PlanValidationRun.agent_request_id == agent_request_id)
            .order_by(
                PlanValidationRun.created_at.desc(),
                PlanValidationRun.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_waiting_confirmation_for_plan(
        self,
        *,
        agent_request_id: uuid.UUID,
        plan_generation_run_id: uuid.UUID,
        plan_hash: str,
        policy_snapshot: dict[str, Any],
    ) -> PlanValidationRun | None:
        """Latest WAITING_CONFIRMATION evidence for exact plan + policy snapshot."""
        stmt = (
            select(PlanValidationRun)
            .where(
                PlanValidationRun.agent_request_id == agent_request_id,
                PlanValidationRun.decision == "WAITING_CONFIRMATION",
                PlanValidationRun.confirmation_required.is_(True),
                PlanValidationRun.plan_generation_run_id == plan_generation_run_id,
                PlanValidationRun.plan_hash == plan_hash,
                PlanValidationRun.clarification_request_id.is_not(None),
            )
            .order_by(
                PlanValidationRun.created_at.desc(),
                PlanValidationRun.id.desc(),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if row.policy_snapshot != policy_snapshot:
            return None
        return row
