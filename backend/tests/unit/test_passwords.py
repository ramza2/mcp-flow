"""Unit tests for password hashing helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.auth.bootstrap import _read_password
from app.auth.passwords import (
    DUMMY_PASSWORD_HASH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
    verify_with_dummy,
)


def test_min_password_length_is_eight() -> None:
    assert MIN_PASSWORD_LENGTH == 8
    assert MAX_PASSWORD_LENGTH == 1024


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


def test_bootstrap_rejects_password_shorter_than_min(tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_text("1234567", encoding="utf-8")  # 7 chars
    with pytest.raises(SystemExit, match="at least 8"):
        _read_password(password_file=str(path))


def test_bootstrap_accepts_password_at_min_length(tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_text("12345678", encoding="utf-8")  # 8 chars
    assert _read_password(password_file=str(path)) == "12345678"


def test_bootstrap_accepts_password_longer_than_min(tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_text("longer-than-eight", encoding="utf-8")
    assert _read_password(password_file=str(path)) == "longer-than-eight"
