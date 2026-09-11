"""Embedding Profile repository (docs/05 embedding_profiles)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_profile import EmbeddingProfile
from app.schemas.common_page import ALLOWED_MODEL_PROFILE_SORT, parse_sort


class EmbeddingProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base(self) -> Select[tuple[EmbeddingProfile]]:
        return select(EmbeddingProfile)

    async def create(
        self,
        *,
        code: str,
        name: str,
        provider: str,
        model: str,
        base_url: str,
        dimension: int,
        distance_metric: str,
        credential_secret_id: uuid.UUID | None = None,
        is_active_for_tools: bool = False,
    ) -> EmbeddingProfile:
        profile = EmbeddingProfile(
            code=code,
            name=name,
            provider=provider,
            model=model,
            base_url=base_url,
            dimension=dimension,
            distance_metric=distance_metric,
            credential_secret_id=credential_secret_id,
            status=None,
            is_active_for_tools=is_active_for_tools,
        )
        self._session.add(profile)
        await self._session.flush()
        await self._session.refresh(profile)
        return profile

    async def get(self, profile_id: uuid.UUID) -> EmbeddingProfile | None:
        result = await self._session.execute(
            self._base().where(EmbeddingProfile.id == profile_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> EmbeddingProfile | None:
        result = await self._session.execute(
            self._base().where(EmbeddingProfile.code == code)
        )
        return result.scalar_one_or_none()

    async def lock_for_update(self, profile_id: uuid.UUID) -> EmbeddingProfile | None:
        stmt = (
            self._base().where(EmbeddingProfile.id == profile_id).with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def lock_active_for_tools(self) -> EmbeddingProfile | None:
        stmt = (
            self._base()
            .where(EmbeddingProfile.is_active_for_tools.is_(True))
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_tools(self) -> EmbeddingProfile | None:
        """Return the active tool EmbeddingProfile without row lock."""
        stmt = self._base().where(EmbeddingProfile.is_active_for_tools.is_(True))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_active_for_tools(self) -> int:
        stmt = (
            select(func.count())
            .select_from(EmbeddingProfile)
            .where(EmbeddingProfile.is_active_for_tools.is_(True))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[EmbeddingProfile], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_MODEL_PROFILE_SORT)
        stmt = self._base()
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    EmbeddingProfile.code.ilike(pattern),
                    EmbeddingProfile.name.ilike(pattern),
                    EmbeddingProfile.provider.ilike(pattern),
                    EmbeddingProfile.model.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(EmbeddingProfile, field)
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
    ) -> EmbeddingProfile | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(EmbeddingProfile, key)
            and key
            not in {
                "id",
                "code",
                "lock_version",
                "status",
                "is_active_for_tools",
                "created_at",
            }
        }
        values["lock_version"] = EmbeddingProfile.lock_version + 1
        values["updated_at"] = func.now()
        stmt = (
            update(EmbeddingProfile)
            .where(
                EmbeddingProfile.id == profile_id,
                EmbeddingProfile.lock_version == expected_lock_version,
            )
            .values(**values)
            .returning(EmbeddingProfile)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_active_flag(
        self,
        profile_id: uuid.UUID,
        *,
        expected_lock_version: int,
        is_active_for_tools: bool,
    ) -> EmbeddingProfile | None:
        stmt = (
            update(EmbeddingProfile)
            .where(
                EmbeddingProfile.id == profile_id,
                EmbeddingProfile.lock_version == expected_lock_version,
            )
            .values(
                is_active_for_tools=is_active_for_tools,
                lock_version=EmbeddingProfile.lock_version + 1,
                updated_at=func.now(),
            )
            .returning(EmbeddingProfile)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
