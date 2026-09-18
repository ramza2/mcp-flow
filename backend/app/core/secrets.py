"""Secret resolution boundary (docs/05 §5.3).

Shared by MCP and Model Provider adapters.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.secret_crypto import decrypt_secret_payload
from app.domain.enums import SecretStatus
from app.repositories.secret import SecretRecordRepository

logger = logging.getLogger(__name__)

MasterKeyLoader = Callable[[], bytes | None]


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """Opaque resolved credential material — never log or return via API."""

    secret_id: uuid.UUID
    kind: str
    material: dict[str, str]


class SecretResolver(Protocol):
    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        """Return resolved secret material, or None if unavailable."""


class UnimplementedSecretResolver:
    """Default resolver until Secret Store lands — always unavailable."""

    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        return None


class DatabaseSecretResolver:
    """Decrypt ACTIVE secret_records with an external AES-256-GCM master key.

    Prefer ``master_key_loader`` for lazy loading so claim→runner paths that
    never need secrets (auth NONE, no SECRET_REF) do not touch the key file.
    When a secret is required, loader failures propagate as ``AppError`` and
    become safe pre-send failures rather than stranding a claimed Execution.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        master_key: bytes | None = None,
        master_key_loader: MasterKeyLoader | None = None,
    ) -> None:
        if master_key is not None and master_key_loader is not None:
            raise ValueError("Provide master_key or master_key_loader, not both.")
        self._session = session
        self._master_key = master_key
        self._master_key_loader = master_key_loader
        self._master_key_resolved = master_key_loader is None
        self._records = SecretRecordRepository(session)

    def _ensure_master_key(self) -> bytes | None:
        if not self._master_key_resolved:
            assert self._master_key_loader is not None
            # May raise AppError (missing/invalid configured key).
            self._master_key = self._master_key_loader()
            self._master_key_resolved = True
        return self._master_key

    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        master_key = self._ensure_master_key()
        if master_key is None:
            logger.info("secret resolve skipped: master key unavailable")
            return None
        row = await self._records.get(secret_id)
        if row is None:
            return None
        if row.status != SecretStatus.ACTIVE.value:
            return None
        if row.expires_at is not None:
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                return None
        try:
            material = decrypt_secret_payload(
                master_key,
                kind=row.secret_kind,
                ciphertext=bytes(row.ciphertext),
                nonce=bytes(row.nonce),
                key_version=row.key_version,
            )
        except AppError:
            logger.info("secret resolve failed closed secret_id=%s", secret_id)
            return None
        return ResolvedSecret(
            secret_id=row.id,
            kind=row.secret_kind,
            material=material,
        )
