"""AES-256-GCM secret encryption — docs/05 §5.3 / ADR-010.

Master key is loaded from an external file path only (never source/Git/DB).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.errors import AppError
from app.domain.enums import SecretKind

_NONCE_BYTES = 12
_KEY_BYTES = 32
_CURRENT_KEY_VERSION = 1


@dataclass(frozen=True, slots=True)
class EncryptedSecretBlob:
    ciphertext: bytes
    nonce: bytes
    key_version: int
    fingerprint: str


def load_master_key_from_file(path: str | Path) -> bytes:
    """Load a 32-byte AES key from file (raw 32 bytes or base64)."""
    key_path = Path(path)
    if not key_path.is_file():
        raise AppError(
            code="SECRET_MASTER_KEY_UNAVAILABLE",
            message="Secret master key file is unavailable.",
            status_code=503,
        )
    raw = key_path.read_bytes().strip()
    if len(raw) == _KEY_BYTES:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise AppError(
            code="SECRET_MASTER_KEY_INVALID",
            message="Secret master key file is invalid.",
            status_code=503,
        ) from exc
    if len(decoded) != _KEY_BYTES:
        raise AppError(
            code="SECRET_MASTER_KEY_INVALID",
            message="Secret master key must be 32 bytes.",
            status_code=503,
        )
    return decoded


def load_master_key_from_settings(*, file_path: str | None) -> bytes | None:
    if not file_path:
        env_path = os.environ.get("MCPFLOW_SECRET_MASTER_KEY_FILE", "").strip()
        file_path = env_path or None
    if not file_path:
        return None
    return load_master_key_from_file(file_path)


def validate_secret_payload(kind: str, material: dict[str, Any]) -> dict[str, str]:
    """Validate decrypted payload shape (docs/05 §5.3). Returns string-only map."""
    if not isinstance(material, dict) or not material:
        raise AppError(
            code="SECRET_PAYLOAD_INVALID",
            message="Secret payload must be a non-empty object.",
            status_code=409,
        )
    for key, value in material.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AppError(
                code="SECRET_PAYLOAD_INVALID",
                message="Secret payload values must be strings.",
                status_code=409,
            )
    try:
        secret_kind = SecretKind(kind)
    except ValueError as exc:
        raise AppError(
            code="SECRET_PAYLOAD_INVALID",
            message="Unknown secret_kind.",
            status_code=409,
        ) from exc

    if secret_kind == SecretKind.API_KEY:
        if set(material.keys()) != {"value"} or not material["value"]:
            raise AppError(
                code="SECRET_PAYLOAD_INVALID",
                message="API_KEY payload requires non-empty value.",
                status_code=409,
            )
    elif secret_kind == SecretKind.BASIC_AUTH:
        if set(material.keys()) != {"username", "password"}:
            raise AppError(
                code="SECRET_PAYLOAD_INVALID",
                message="BASIC_AUTH payload requires username and password.",
                status_code=409,
            )
        if not material["username"] or not material["password"]:
            raise AppError(
                code="SECRET_PAYLOAD_INVALID",
                message="BASIC_AUTH username/password must be non-empty.",
                status_code=409,
            )
    elif secret_kind == SecretKind.OAUTH_TOKEN_SET:
        if set(material.keys()) != {"access_token"} or not material["access_token"]:
            raise AppError(
                code="SECRET_PAYLOAD_INVALID",
                message="OAUTH_TOKEN_SET payload requires non-empty access_token.",
                status_code=409,
            )
    # CUSTOM: any string→string map already validated above.
    return {str(k): str(v) for k, v in material.items()}


def fingerprint_plaintext(master_key: bytes, plaintext: bytes) -> str:
    digest = hmac.new(master_key, plaintext, hashlib.sha256).hexdigest()
    return digest


def encrypt_secret_payload(
    master_key: bytes,
    *,
    kind: str,
    material: dict[str, Any],
    key_version: int = _CURRENT_KEY_VERSION,
) -> EncryptedSecretBlob:
    validated = validate_secret_payload(kind, material)
    plaintext = json.dumps(validated, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(master_key).encrypt(nonce, plaintext, associated_data=None)
    return EncryptedSecretBlob(
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=key_version,
        fingerprint=fingerprint_plaintext(master_key, plaintext),
    )


def decrypt_secret_payload(
    master_key: bytes,
    *,
    kind: str,
    ciphertext: bytes,
    nonce: bytes,
    key_version: int,
) -> dict[str, str]:
    if key_version != _CURRENT_KEY_VERSION:
        raise AppError(
            code="SECRET_KEY_VERSION_UNKNOWN",
            message="Unknown secret key_version.",
            status_code=409,
        )
    if len(nonce) != _NONCE_BYTES:
        raise AppError(
            code="SECRET_DECRYPT_FAILED",
            message="Secret decrypt failed.",
            status_code=409,
        )
    try:
        plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, associated_data=None)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        # Never include ciphertext/nonce/plaintext in the error message.
        raise AppError(
            code="SECRET_DECRYPT_FAILED",
            message="Secret decrypt failed.",
            status_code=409,
        ) from exc
    if not isinstance(payload, dict):
        raise AppError(
            code="SECRET_PAYLOAD_INVALID",
            message="Secret payload must be an object.",
            status_code=409,
        )
    return validate_secret_payload(kind, payload)
