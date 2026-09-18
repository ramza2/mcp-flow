"""Safety tests for infra/scripts/generate_local_secrets.py."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "scripts"
    / "generate_local_secrets.py"
)


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_local_secrets", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(module, argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = stdout, stderr
        code = module.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, stdout.getvalue(), stderr.getvalue()


def _assert_no_secret_leak(output: str, *paths: Path) -> None:
    for path in paths:
        raw = path.read_text(encoding="utf-8").strip()
        assert raw
        assert raw not in output
        # Also guard against accidental base64/password fragments in status lines
        # by requiring output only mentions filenames/status words.
    assert "values not shown" in output or "WARNING:" in output or output == ""


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    return tmp_path / "secrets"


def test_empty_dir_creates_all_six(secrets_dir: Path) -> None:
    gen = _load_generator()
    code, out, err = _run(gen, ["--dir", str(secrets_dir)])
    assert code == 0
    assert "written=6" in out
    assert "EXISTING" not in out
    for name in gen.SECRET_FILES:
        path = secrets_dir / name
        assert path.is_file() and path.stat().st_size > 0
        assert name in out
        assert path.read_text(encoding="utf-8").strip() not in out
        assert path.read_text(encoding="utf-8").strip() not in err
    assert err == ""


def test_second_normal_run_skips_all(secrets_dir: Path) -> None:
    gen = _load_generator()
    assert _run(gen, ["--dir", str(secrets_dir)])[0] == 0
    before = {name: _sha256(secrets_dir / name) for name in gen.SECRET_FILES}

    code, out, err = _run(gen, ["--dir", str(secrets_dir)])
    assert code == 0
    assert "written=0" in out
    assert "skipped=6" in out
    for name in gen.SECRET_FILES:
        assert before[name] == _sha256(secrets_dir / name)
        assert (secrets_dir / name).read_text(encoding="utf-8").strip() not in out
    assert err == ""


def test_force_rotates_passwords_but_preserves_master_key(secrets_dir: Path) -> None:
    gen = _load_generator()
    assert _run(gen, ["--dir", str(secrets_dir)])[0] == 0
    before = {name: _sha256(secrets_dir / name) for name in gen.SECRET_FILES}
    master_before = before["secret_master_key"]

    code, out, err = _run(gen, ["--dir", str(secrets_dir), "--force"])
    assert code == 0
    assert "secret_master_key: skipped" in out
    after = {name: _sha256(secrets_dir / name) for name in gen.SECRET_FILES}
    assert after["secret_master_key"] == master_before
    for name in gen.SECRET_FILES:
        if name == "secret_master_key":
            continue
        assert after[name] != before[name]
        assert (secrets_dir / name).read_text(encoding="utf-8").strip() not in out
        assert (secrets_dir / name).read_text(encoding="utf-8").strip() not in err
    assert (secrets_dir / "secret_master_key").read_text(encoding="utf-8").strip() not in out
    assert err == ""


def test_force_master_key_overwrites_only_when_explicit(secrets_dir: Path) -> None:
    gen = _load_generator()
    assert _run(gen, ["--dir", str(secrets_dir)])[0] == 0
    before = {name: _sha256(secrets_dir / name) for name in gen.SECRET_FILES}

    code, out, err = _run(gen, ["--dir", str(secrets_dir), "--force-master-key"])
    assert code == 0
    assert "WARNING" in err
    assert "secret_master_key: written" in out
    after = {name: _sha256(secrets_dir / name) for name in gen.SECRET_FILES}
    assert after["secret_master_key"] != before["secret_master_key"]
    for name in gen.SECRET_FILES:
        if name == "secret_master_key":
            continue
        assert after[name] == before[name]
    for name in gen.SECRET_FILES:
        material = (secrets_dir / name).read_text(encoding="utf-8").strip()
        assert material not in out
        assert material not in err
