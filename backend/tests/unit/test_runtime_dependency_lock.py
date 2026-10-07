"""Ensure pyproject runtime deps are exactly pinned in requirements.lock.

Catches Dockerfile drift: runtime installs ``pip install -r requirements.lock``
then ``pip install --no-deps .``, so lock omissions become ModuleNotFoundError.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = BACKEND_ROOT / "pyproject.toml"
LOCK = BACKEND_ROOT / "requirements.lock"

_LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==(?P<version>[^\s#]+)\s*$"
)


def _runtime_requirements() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    assert isinstance(deps, list) and deps, "pyproject.toml has no [project].dependencies"
    return [Requirement(str(item)) for item in deps]


def _lock_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.match(line)
        assert match is not None, f"requirements.lock line is not name==version: {raw!r}"
        name = canonicalize_name(match.group("name"))
        version = match.group("version")
        # First pin wins; extras forms share the canonical package name.
        pins.setdefault(name, version)
    assert pins, "requirements.lock is empty"
    return pins


def test_every_runtime_dependency_is_exactly_pinned_in_lock() -> None:
    pins = _lock_pins()
    missing: list[str] = []
    mismatched: list[str] = []

    for req in _runtime_requirements():
        name = canonicalize_name(req.name)
        pinned = pins.get(name)
        if pinned is None:
            missing.append(name)
            continue
        if not req.specifier.contains(Version(pinned), prereleases=True):
            mismatched.append(
                f"{name}: lock={pinned!r} does not satisfy {str(req.specifier)!r}"
            )

    assert not missing, (
        "Runtime dependencies missing exact == pins in requirements.lock: "
        + ", ".join(sorted(missing))
    )
    assert not mismatched, (
        "Runtime lock pins do not satisfy pyproject specifiers: "
        + "; ".join(sorted(mismatched))
    )


def test_croniter_runtime_pin_present() -> None:
    """Focused guard for the production ModuleNotFoundError that shipped."""
    pins = _lock_pins()
    assert "croniter" in pins, "croniter must be pinned in requirements.lock"
    assert pins["croniter"] == "6.2.4"


@pytest.mark.parametrize(
    "req_str,expected",
    [
        ("celery[redis]>=5.6.3,<6.0.0", "celery"),
        ("uvicorn[standard]>=0.32.0,<1.0.0", "uvicorn"),
        ("SQLAlchemy[asyncio]>=2.0.36,<3.0.0", "sqlalchemy"),
    ],
)
def test_canonicalize_extra_requirement_names(req_str: str, expected: str) -> None:
    assert canonicalize_name(Requirement(req_str).name) == expected
