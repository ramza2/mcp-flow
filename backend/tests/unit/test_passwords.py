"""Unit tests for password hashing helpers."""

from __future__ import annotations

import pytest
from app.auth.passwords import (
    DUMMY_PASSWORD_HASH,
    MAX_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
    verify_with_dummy,
)


def test_hash_and_verify_roundtrip() -> None:
    encoded = hash_password("correct-horse-battery-staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password("correct-horse-battery-staple", encoded)
    assert not verify_password("wrong-password-xxxx", encoded)
    assert "correct-horse" not in encoded


def test_dummy_verify_uses_fixed_hash() -> None:
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert verify_with_dummy("any-password-value") is False
    assert isinstance(needs_rehash(DUMMY_PASSWORD_HASH), bool)


def test_password_max_length() -> None:
    with pytest.raises(ValueError):
        hash_password("x" * (MAX_PASSWORD_LENGTH + 1))
    assert verify_password("x" * (MAX_PASSWORD_LENGTH + 1), DUMMY_PASSWORD_HASH) is False
