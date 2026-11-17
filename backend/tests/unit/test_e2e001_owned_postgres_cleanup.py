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


def _postgres_bins_available() -> bool:
    mod = _load_harness()
    try:
        mod._pg_bin("initdb")
        mod._pg_bin("pg_ctl")
        mod._pg_bin("createdb")
    except RuntimeError:
        return False
    return True


def _short_state_dir() -> Path:
    path = Path("/tmp") / f"e2t{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pg_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("pg-*") if p.is_dir())


def test_createdb_failure_calls_stop_via_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real Postgres binaries required — verifies cleanup hook on createdb fail."""
    mod = _load_harness()
    state_dir = _short_state_dir()
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "_pg_bin", lambda name: f"/mock/{name}")
    monkeypatch.setattr(mod, "_wait_tcp", lambda *_a, **_k: None)
    stop_calls: list[Path | None] = []

    def tracking_stop(data_dir: Path | None) -> None:
        stop_calls.append(data_dir)
        if data_dir is not None and data_dir.is_dir():
            marker = data_dir / ".e2e001_sock_dir"
            if marker.is_file():
                sock = Path(marker.read_text(encoding="utf-8").strip())
                shutil.rmtree(sock, ignore_errors=True)
            shutil.rmtree(data_dir, ignore_errors=True)

    monkeypatch.setattr(mod, "_stop_dedicated_postgres", tracking_stop)

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001
        cmd = Path(str(argv[0])).name if argv else ""
        if cmd == "initdb":
            data = Path(argv[argv.index("-D") + 1])
            data.mkdir(parents=True, exist_ok=True)
            (data / "postgresql.conf").write_text("# mock\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd == "pg_ctl" and "start" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd == "createdb":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="forced createdb failure")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    try:
        with pytest.raises(RuntimeError, match="createdb failed"):
            mod._start_dedicated_postgres()
        assert len(stop_calls) == 1
        assert stop_calls[0] is not None
        assert _pg_dirs(state_dir) == []
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_ready_failure_calls_stop_via_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_harness()
    state_dir = _short_state_dir()
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "_pg_bin", lambda name: f"/mock/{name}")

    def boom(_host: str, _port: int, timeout_s: float = 30.0) -> None:
        raise RuntimeError("forced ready timeout")

    monkeypatch.setattr(mod, "_wait_tcp", boom)
    stop_calls: list[Path | None] = []

    def tracking_stop(data_dir: Path | None) -> None:
        stop_calls.append(data_dir)
        if data_dir is not None and data_dir.is_dir():
            marker = data_dir / ".e2e001_sock_dir"
            if marker.is_file():
                sock = Path(marker.read_text(encoding="utf-8").strip())
                shutil.rmtree(sock, ignore_errors=True)
            shutil.rmtree(data_dir, ignore_errors=True)

    monkeypatch.setattr(mod, "_stop_dedicated_postgres", tracking_stop)

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001
        cmd = Path(str(argv[0])).name if argv else ""
        if cmd == "initdb":
            data = Path(argv[argv.index("-D") + 1])
            data.mkdir(parents=True, exist_ok=True)
            (data / "postgresql.conf").write_text("# mock\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd == "pg_ctl" and "start" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    try:
        with pytest.raises(RuntimeError, match="forced ready timeout"):
            mod._start_dedicated_postgres()
        assert len(stop_calls) == 1
        assert _pg_dirs(state_dir) == []
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.mark.skipif(
    not _postgres_bins_available(),
    reason="PostgreSQL server binaries (initdb/pg_ctl/createdb) not installed",
)
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


@pytest.mark.skipif(
    not _postgres_bins_available(),
    reason="PostgreSQL server binaries (initdb/pg_ctl/createdb) not installed",
)
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
