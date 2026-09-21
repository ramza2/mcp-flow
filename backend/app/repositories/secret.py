"""Secret record repository — ciphertext-only persistence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.secret import SecretRecord


class SecretRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, secret_id: uuid.UUID) -> SecretRecord | None:
        stmt = select(SecretRecord).where(SecretRecord.id == secret_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        name: str,
        secret_kind: str,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
        fingerprint: str,
        status: str,
        expires_at: datetime | None = None,
        secret_id: uuid.UUID | None = None,
    ) -> SecretRecord:
        row = SecretRecord(
            id=secret_id or uuid.uuid4(),
            name=name,
            secret_kind=secret_kind,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            fingerprint=fingerprint,
            status=status,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row
