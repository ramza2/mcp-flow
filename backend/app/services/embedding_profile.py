"""Embedding Profile lifecycle service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.url_validation import validate_model_base_url
from app.model_provider.client import EmbeddingConnectionTarget, ModelProviderClient
from app.models.model_profile import EmbeddingProfile
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.model_profile import (
    EmbeddingProfileCreate,
    EmbeddingProfileUpdate,
    ModelProfileConnectionTestResponse,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Fields that change embedding generation semantics (docs/05 §8.6).
_EMBEDDING_GENERATION_FIELDS = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "dimension",
        "credential_secret_id",
    }
)


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "emb")


def _generate_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


class EmbeddingProfileService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model_provider: ModelProviderClient | None = None,
    ) -> None:
        self._session = session
        self._profiles = EmbeddingProfileRepository(session)
        self._provider = model_provider

    async def _require(self, profile_id: uuid.UUID) -> EmbeddingProfile:
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise AppError(
                code="NOT_FOUND",
                message="Embedding profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return profile

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="Embedding profile lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: EmbeddingProfileCreate) -> EmbeddingProfile:
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
            dimension=data.dimension,
            distance_metric=data.distance_metric,
            credential_secret_id=data.credential_secret_id,
            is_active_for_tools=False,
        )
        await self._session.commit()
        await self._session.refresh(profile)
        return profile

    async def get(self, profile_id: uuid.UUID) -> EmbeddingProfile:
        return await self._require(profile_id)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[EmbeddingProfile], int]:
        return await self._profiles.list(
            page=page, page_size=page_size, q=q, sort=sort
        )

    async def update(
        self,
        profile_id: uuid.UUID,
        data: EmbeddingProfileUpdate,
        *,
        expected_lock_version: int,
    ) -> EmbeddingProfile:
        # Lock first so stale If-Match is evaluated before domain guards.
        profile = await self._profiles.lock_for_update(profile_id)
        if profile is None:
            raise AppError(
                code="NOT_FOUND",
                message="Embedding profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if profile.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if "base_url" in payload and payload["base_url"] is not None:
            payload["base_url"] = validate_model_base_url(payload["base_url"])

        if (
            "dimension" in payload
            and payload["dimension"] is not None
            and int(payload["dimension"]) != int(profile.dimension)
            and profile.is_active_for_tools
        ):
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Active Tool-search Embedding Profile dimension cannot be changed. "
                    "Activate another profile before changing dimension."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        if not payload:
            await self._session.commit()
            return profile

        stale_needed = any(
            field in _EMBEDDING_GENERATION_FIELDS
            and payload[field] != getattr(profile, field)
            for field in payload
        )

        updated = await self._profiles.update_atomic(
            profile_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            await self._session.rollback()
            current = await self._profiles.get(profile_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Embedding profile not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        if stale_needed:
            await ToolEmbeddingRepository(self._session).mark_stale_for_profile(
                profile_id
            )

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate_for_tools(
        self,
        profile_id: uuid.UUID,
        *,
        expected_lock_version: int,
    ) -> EmbeddingProfile:
        target = await self._profiles.lock_for_update(profile_id)
        if target is None:
            raise AppError(
                code="NOT_FOUND",
                message="Embedding profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if target.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        # Idempotent: already active with matching If-Match — no lock bump.
        # Commit releases FOR UPDATE so other sessions are not blocked.
        if target.is_active_for_tools:
            await self._session.commit()
            return target

        previous = await self._profiles.lock_active_for_tools()
        try:
            if previous is not None and previous.id != target.id:
                deactivated = await self._profiles.set_active_flag(
                    previous.id,
                    expected_lock_version=int(previous.lock_version),
                    is_active_for_tools=False,
                )
                if deactivated is None:
                    await self._session.rollback()
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Concurrent embedding activation conflict; retry.",
                        status_code=status.HTTP_409_CONFLICT,
                    )
            activated = await self._profiles.set_active_flag(
                target.id,
                expected_lock_version=int(target.lock_version),
                is_active_for_tools=True,
            )
            if activated is None:
                await self._session.rollback()
                raise AppError(
                    code="RESOURCE_VERSION_CONFLICT",
                    message="Embedding profile lock_version does not match.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            await self._session.commit()
            await self._session.refresh(activated)
            return activated
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Concurrent embedding activation conflict; retry.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def connection_test(
        self, profile_id: uuid.UUID
    ) -> ModelProfileConnectionTestResponse:
        profile = await self._require(profile_id)
        await self._session.commit()
        client = self._provider or ModelProviderClient()
        owns = self._provider is None
        try:
            return await client.test_embedding(
                EmbeddingConnectionTarget(
                    provider=profile.provider,
                    model=profile.model,
                    base_url=profile.base_url,
                    dimension=int(profile.dimension),
                    credential_secret_id=profile.credential_secret_id,
                )
            )
        finally:
            if owns:
                await client.aclose()
