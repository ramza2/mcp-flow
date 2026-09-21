"""Unit tests for AES-256-GCM secret crypto and DatabaseSecretResolver (docs/05 §5.3)."""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.errors import AppError
from app.core.secret_crypto import (
    decrypt_secret_payload,
    encrypt_secret_payload,
    load_master_key_from_file,
    load_master_key_from_settings,
)
from app.core.secrets import DatabaseSecretResolver
from app.domain.enums import SecretKind, SecretStatus
from app.repositories.secret import SecretRecordRepository
from sqlalchemy.ext.asyncio import AsyncSession


def _random_key_bytes() -> bytes:
    """32 random bytes guaranteed to survive ``bytes.strip()`` round-trip."""
    while True:
        candidate = os.urandom(32)
        if candidate.strip() == candidate:
            return candidate


def _write_master_key(tmp_path: Path, *, raw: bytes | None = None) -> Path:
    key = raw if raw is not None else _random_key_bytes()
    path = tmp_path / "master.key"
    path.write_bytes(key)
    return path


async def _seed_secret_record(
    session: AsyncSession,
    *,
    master_key: bytes,
    kind: str = SecretKind.API_KEY.value,
    material: dict[str, str] | None = None,
    status: str = SecretStatus.ACTIVE.value,
    expires_at: datetime | None = None,
):
    blob = encrypt_secret_payload(
        master_key,
        kind=kind,
        material=material or {"value": "sk-super-secret-token"},
    )
    return await SecretRecordRepository(session).create(
        name=f"secret-{uuid.uuid4().hex[:8]}",
        secret_kind=kind,
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        key_version=blob.key_version,
        fingerprint=blob.fingerprint,
        status=status,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# encrypt/decrypt round-trip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip_api_key(tmp_path: Path) -> None:
    master_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": "sk-abc123"}
    )
    assert blob.ciphertext != b""
    assert len(blob.nonce) == 12
    assert blob.key_version == 1

    decrypted = decrypt_secret_payload(
        master_key,
        kind=SecretKind.API_KEY.value,
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        key_version=blob.key_version,
    )
    assert decrypted == {"value": "sk-abc123"}
    # Ciphertext must never contain the plaintext secret value.
    assert b"sk-abc123" not in blob.ciphertext


def test_encrypt_decrypt_round_trip_basic_auth() -> None:
    master_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key,
        kind=SecretKind.BASIC_AUTH.value,
        material={"username": "svc", "password": "p@ss"},
    )
    decrypted = decrypt_secret_payload(
        master_key,
        kind=SecretKind.BASIC_AUTH.value,
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        key_version=blob.key_version,
    )
    assert decrypted == {"username": "svc", "password": "p@ss"}


def test_load_master_key_from_file_raw_bytes(tmp_path: Path) -> None:
    raw = _random_key_bytes()
    path = _write_master_key(tmp_path, raw=raw)
    assert load_master_key_from_file(path) == raw


def test_load_master_key_from_file_base64(tmp_path: Path) -> None:
    raw = os.urandom(32)
    path = tmp_path / "master.key.b64"
    path.write_bytes(base64.b64encode(raw))
    assert load_master_key_from_file(path) == raw


def test_load_master_key_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AppError) as exc:
        load_master_key_from_file(tmp_path / "does-not-exist.key")
    assert exc.value.code == "SECRET_MASTER_KEY_UNAVAILABLE"
    assert exc.value.status_code == 503


def test_load_master_key_wrong_length_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.key"
    path.write_bytes(os.urandom(16))
    with pytest.raises(AppError) as exc:
        load_master_key_from_file(path)
    assert exc.value.code == "SECRET_MASTER_KEY_INVALID"


def test_load_master_key_from_settings_none_when_unset() -> None:
    assert load_master_key_from_settings(file_path=None) is None


def test_load_master_key_from_settings_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _random_key_bytes()
    path = _write_master_key(tmp_path, raw=raw)
    monkeypatch.setenv("MCPFLOW_SECRET_MASTER_KEY_FILE", str(path))
    assert load_master_key_from_settings(file_path=None) == raw


# ---------------------------------------------------------------------------
# Invalid payload fails closed
# ---------------------------------------------------------------------------


def test_decrypt_invalid_key_version_fails_closed() -> None:
    master_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": "x"}
    )
    with pytest.raises(AppError) as exc:
        decrypt_secret_payload(
            master_key,
            kind=SecretKind.API_KEY.value,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            key_version=99,
        )
    assert exc.value.code == "SECRET_KEY_VERSION_UNKNOWN"


def test_decrypt_invalid_nonce_length_fails_closed() -> None:
    master_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": "x"}
    )
    with pytest.raises(AppError) as exc:
        decrypt_secret_payload(
            master_key,
            kind=SecretKind.API_KEY.value,
            ciphertext=blob.ciphertext,
            nonce=b"short",
            key_version=blob.key_version,
        )
    assert exc.value.code == "SECRET_DECRYPT_FAILED"


def test_decrypt_wrong_key_fails_closed_and_never_leaks_ciphertext() -> None:
    master_key = os.urandom(32)
    wrong_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": "sk-abc123"}
    )
    with pytest.raises(AppError) as exc:
        decrypt_secret_payload(
            wrong_key,
            kind=SecretKind.API_KEY.value,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            key_version=blob.key_version,
        )
    assert exc.value.code == "SECRET_DECRYPT_FAILED"
    # AppError message must never include ciphertext/nonce/plaintext material.
    assert "sk-abc123" not in exc.value.message
    assert blob.ciphertext.hex() not in exc.value.message
    assert blob.nonce.hex() not in exc.value.message
    assert str(blob.ciphertext) not in exc.value.message


def test_decrypt_tampered_ciphertext_fails_closed() -> None:
    master_key = os.urandom(32)
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": "sk-abc123"}
    )
    tampered = bytes([blob.ciphertext[0] ^ 0xFF]) + blob.ciphertext[1:]
    with pytest.raises(AppError) as exc:
        decrypt_secret_payload(
            master_key,
            kind=SecretKind.API_KEY.value,
            ciphertext=tampered,
            nonce=blob.nonce,
            key_version=blob.key_version,
        )
    assert exc.value.code == "SECRET_DECRYPT_FAILED"
    assert "sk-abc123" not in exc.value.message


def test_encrypt_rejects_empty_api_key_payload() -> None:
    master_key = os.urandom(32)
    with pytest.raises(AppError) as exc:
        encrypt_secret_payload(
            master_key, kind=SecretKind.API_KEY.value, material={"value": ""}
        )
    assert exc.value.code == "SECRET_PAYLOAD_INVALID"


def test_encrypt_rejects_unknown_secret_kind() -> None:
    master_key = os.urandom(32)
    with pytest.raises(AppError) as exc:
        encrypt_secret_payload(
            master_key, kind="NOT_A_KIND", material={"value": "x"}
        )
    assert exc.value.code == "SECRET_PAYLOAD_INVALID"


# ---------------------------------------------------------------------------
# DatabaseSecretResolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_active_secret_ok(db_session: AsyncSession) -> None:
    master_key = os.urandom(32)
    record = await _seed_secret_record(db_session, master_key=master_key)
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=master_key)
    resolved = await resolver.resolve(record.id)
    assert resolved is not None
    assert resolved.secret_id == record.id
    assert resolved.kind == SecretKind.API_KEY.value
    assert resolved.material == {"value": "sk-super-secret-token"}


@pytest.mark.asyncio
async def test_resolver_revoked_secret_returns_none(db_session: AsyncSession) -> None:
    master_key = os.urandom(32)
    record = await _seed_secret_record(
        db_session, master_key=master_key, status=SecretStatus.REVOKED.value
    )
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=master_key)
    assert await resolver.resolve(record.id) is None


@pytest.mark.asyncio
async def test_resolver_expired_status_returns_none(db_session: AsyncSession) -> None:
    master_key = os.urandom(32)
    record = await _seed_secret_record(
        db_session, master_key=master_key, status=SecretStatus.EXPIRED.value
    )
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=master_key)
    assert await resolver.resolve(record.id) is None


@pytest.mark.asyncio
async def test_resolver_active_but_expires_at_in_past_returns_none(
    db_session: AsyncSession,
) -> None:
    master_key = os.urandom(32)
    record = await _seed_secret_record(
        db_session,
        master_key=master_key,
        status=SecretStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=master_key)
    assert await resolver.resolve(record.id) is None


@pytest.mark.asyncio
async def test_resolver_missing_secret_returns_none(db_session: AsyncSession) -> None:
    master_key = os.urandom(32)
    resolver = DatabaseSecretResolver(db_session, master_key=master_key)
    assert await resolver.resolve(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_resolver_no_master_key_returns_none(db_session: AsyncSession) -> None:
    master_key = os.urandom(32)
    record = await _seed_secret_record(db_session, master_key=master_key)
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=None)
    assert await resolver.resolve(record.id) is None


@pytest.mark.asyncio
async def test_resolver_decrypt_failure_fails_closed_not_500(
    db_session: AsyncSession,
) -> None:
    master_key = os.urandom(32)
    wrong_key = os.urandom(32)
    record = await _seed_secret_record(db_session, master_key=master_key)
    await db_session.commit()

    resolver = DatabaseSecretResolver(db_session, master_key=wrong_key)
    assert await resolver.resolve(record.id) is None
