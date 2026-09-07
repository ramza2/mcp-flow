"""LLM Profile lifecycle service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.url_validation import validate_model_base_url
from app.models.model_profile import LLMProfile
from app.model_provider.client import LLMConnectionTarget, ModelProviderClient
from app.repositories.llm_profile import LLMProfileRepository
from app.schemas.model_profile import (
    LLMProfileCreate,
    LLMProfileUpdate,
    ModelProfileConnectionTestResponse,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "llm")


def _generate_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


class LLMProfileService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model_provider: ModelProviderClient | None = None,
    ) -> None:
        self._session = session
        self._profiles = LLMProfileRepository(session)
        self._provider = model_provider

    async def _require(self, profile_id: uuid.UUID) -> LLMProfile:
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise AppError(
                code="NOT_FOUND",
                message="LLM profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return profile

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="LLM profile lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: LLMProfileCreate) -> LLMProfile:
        base_url = validate_model_base_url(data.base_url)
        code = _generate_code(data.name)
        if await self._profiles.get_by_code(code) is not None:
            code = _generate_code(data.name)
        profile = await self._profiles.create(
            code=code,
            name=data.name,
            provider=data.provider,
            model=data.model,
            base_url=base_url,
            credential_secret_id=data.credential_secret_id,
            parameters=data.parameters,
        )
        await self._session.commit()
        await self._session.refresh(profile)
        return profile

    async def get(self, profile_id: uuid.UUID) -> LLMProfile:
        return await self._require(profile_id)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[LLMProfile], int]:
        return await self._profiles.list(
            page=page, page_size=page_size, q=q, sort=sort
        )

    async def update(
        self,
        profile_id: uuid.UUID,
        data: LLMProfileUpdate,
        *,
        expected_lock_version: int,
    ) -> LLMProfile:
        profile = await self._require(profile_id)
        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if "base_url" in payload and payload["base_url"] is not None:
            payload["base_url"] = validate_model_base_url(payload["base_url"])
        if not payload:
            # No-op patch still verifies CAS version.
            if profile.lock_version != expected_lock_version:
                self._raise_version_conflict()
            return profile
        updated = await self._profiles.update_atomic(
            profile_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            await self._session.rollback()
            # Distinguish missing vs stale.
            current = await self._profiles.get(profile_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="LLM profile not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def connection_test(
        self, profile_id: uuid.UUID
    ) -> ModelProfileConnectionTestResponse:
        profile = await self._require(profile_id)
        # End DB transaction before outbound HTTP.
        await self._session.commit()
        client = self._provider or ModelProviderClient()
        owns = self._provider is None
        try:
            return await client.test_llm(
                LLMConnectionTarget(
                    provider=profile.provider,
                    model=profile.model,
                    base_url=profile.base_url,
                    credential_secret_id=profile.credential_secret_id,
                )
            )
        finally:
            if owns:
                await client.aclose()
