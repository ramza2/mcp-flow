#!/usr/bin/env python3
"""Generate local non-Git secrets for MCPFlow Docker Compose.

Passwords are URL-safe so MCPFLOW_DATABASE_URL assembly needs no escaping.
Existing files are preserved unless --force is passed.
Secret values are never printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = REPO_ROOT / "infra" / "secrets" / "local"

SECRET_FILES = (
    "postgres_admin_password",
    "postgres_migration_password",
    "postgres_app_password",
    "minio_root_user",
    "minio_root_password",
)


def _generate_password() -> str:
    # token_urlsafe is URL-safe (A-Za-z0-9_-) — avoids DB URL escaping issues.
    return secrets.token_urlsafe(32)


def _generate_minio_user() -> str:
    # MinIO access key: printable ASCII without spaces; keep short and URL-safe.
    return f"mcpflow{secrets.token_hex(8)}"


def _write_secret(path: Path, value: str, *, force: bool) -> str:
    if path.exists() and not force:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        # 0644: Compose file secrets are bind-mounted; non-root app/postgres
        # users inside containers must be able to read them. Files remain
        # gitignored and must never hold production credentials.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        # Windows / restricted FS may not support Unix modes.
        pass
    return "written"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate MCPFlow local secrets")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing secret files",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=SECRETS_DIR,
        help="Secrets output directory (default: infra/secrets/local)",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.dir
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}
    for name in SECRET_FILES:
        path = out_dir / name
        if name == "minio_root_user":
            value = _generate_minio_user()
        else:
            value = _generate_password()
        results[name] = _write_secret(path, value, force=args.force)

    written = sum(1 for status in results.values() if status == "written")
    skipped = sum(1 for status in results.values() if status == "skipped")
    print(f"secrets directory: {out_dir}")
    print(f"written={written} skipped={skipped} (values not shown)")
    for name, status in results.items():
        print(f"  {name}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
