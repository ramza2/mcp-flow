"""Owned disposable Postgres must stop/clean on createdb/ready failure."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "e2e001_isolated_stack.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("e2e001_isolated_stack", _HARNESS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _short_state_dir() -> Path:
    # Keep paths short so Postgres unix sockets stay under the 107-byte limit.
    path = Path("/tmp") / f"e2t{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pg_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("pg-*") if p.is_dir())


def test_createdb_failure_stops_and_removes_owned_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_harness()
    state_dir = _short_state_dir()
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001
        cmd = Path(str(argv[0])).name if argv else ""
        if cmd == "createdb":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="forced createdb failure")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    before_socks = set(Path("/tmp").glob("e2pg*"))
    try:
        with pytest.raises(RuntimeError, match="createdb failed"):
            mod._start_dedicated_postgres()
        assert _pg_dirs(state_dir) == []
        assert set(Path("/tmp").glob("e2pg*")) <= before_socks
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_ready_wait_failure_stops_and_removes_owned_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_harness()
    state_dir = _short_state_dir()
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)

    def boom(_host: str, _port: int, timeout_s: float = 30.0) -> None:
        raise RuntimeError("forced ready timeout")

    monkeypatch.setattr(mod, "_wait_tcp", boom)

    before_socks = set(Path("/tmp").glob("e2pg*"))
    try:
        with pytest.raises(RuntimeError, match="forced ready timeout"):
            mod._start_dedicated_postgres()
        assert _pg_dirs(state_dir) == []
        assert set(Path("/tmp").glob("e2pg*")) <= before_socks
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
