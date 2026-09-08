"""Password hashing / verification (Argon2id)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Fixed encoded Argon2id hash used only for login timing equalization.
# Not a real user credential; never regenerated per request.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "v0VW69RpZ0ecKiy2ByPm1Q$"
    "sDa48H8Cq7EqS0NfydEcULhUW/2cbteuUis7mnbjCYo"
)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with Argon2id. Does not strip whitespace."""
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password exceeds maximum length")
    return _hasher.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify password against an encoded Argon2 hash."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        return _hasher.verify(encoded_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(encoded_hash: str) -> bool:
    try:
        return bool(_hasher.check_needs_rehash(encoded_hash))
    except Exception:  # noqa: BLE001 — treat unreadable hashes as needing replacement
        return True


def verify_with_dummy(password: str) -> bool:
    """Run Argon2 verify against a fixed dummy hash (timing equalization)."""
    return verify_password(password, DUMMY_PASSWORD_HASH)
