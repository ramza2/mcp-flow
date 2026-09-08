"""LLM Profile repository (docs/05 llm_profiles)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_profile import LLMProfile
from app.schemas.common_page import ALLOWED_MODEL_PROFILE_SORT, parse_sort


class LLMProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base(self) -> Select[tuple[LLMProfile]]:
        return select(LLMProfile)

    async def create(
        self,
        *,
        code: str,
        name: str,
        provider: str,
        model: str,
        base_url: str,
        credential_secret_id: uuid.UUID | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> LLMProfile:
        profile = LLMProfile(
            code=code,
            name=name,
            provider=provider,
            model=model,
            base_url=base_url,
            credential_secret_id=credential_secret_id,
            parameters=parameters,
            status=None,
        )
        self._session.add(profile)
        await self._session.flush()
        await self._session.refresh(profile)
        return profile

    async def get(self, profile_id: uuid.UUID) -> LLMProfile | None:
        result = await self._session.execute(
            self._base().where(LLMProfile.id == profile_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> LLMProfile | None:
        result = await self._session.execute(
            self._base().where(LLMProfile.code == code)
        )
        return result.scalar_one_or_none()

    async def exists(self, profile_id: uuid.UUID) -> bool:
        stmt = select(func.count()).select_from(LLMProfile).where(
            LLMProfile.id == profile_id
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0

    async def lock_for_update(self, profile_id: uuid.UUID) -> LLMProfile | None:
        stmt = self._base().where(LLMProfile.id == profile_id).with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[LLMProfile], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_MODEL_PROFILE_SORT)
        stmt = self._base()
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    LLMProfile.code.ilike(pattern),
                    LLMProfile.name.ilike(pattern),
                    LLMProfile.provider.ilike(pattern),
                    LLMProfile.model.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(LLMProfile, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(order).offset(offset).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def update_atomic(
        self,
        profile_id: uuid.UUID,
        *,
        expected_lock_version: int,
        **fields: Any,
    ) -> LLMProfile | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(LLMProfile, key)
            and key
            not in {
                "id",
                "code",
                "lock_version",
                "status",
                "created_at",
            }
        }
        values["lock_version"] = LLMProfile.lock_version + 1
        values["updated_at"] = func.now()
        stmt = (
            update(LLMProfile)
            .where(
                LLMProfile.id == profile_id,
                LLMProfile.lock_version == expected_lock_version,
            )
            .values(**values)
            .returning(LLMProfile)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
