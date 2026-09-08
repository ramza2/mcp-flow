"""Operational credential bootstrap CLI (not an HTTP API).

Examples:
  python -m app.auth.bootstrap bootstrap-user --username op --display-name Operator \\
      --email op@example.com --password-file /path/to/pw

  python -m app.auth.bootstrap set-password --username op --password-file /path/to/pw
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
)
from app.core.config import get_settings
from app.domain.enums import UserStatus
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository


def _read_password(*, password_file: str | None) -> str:
    if password_file:
        raw = Path(password_file).read_text(encoding="utf-8")
        # Preserve internal whitespace; only strip a single trailing newline
        # commonly present in password files.
        if raw.endswith("\n"):
            raw = raw[:-1]
        if raw.endswith("\r"):
            raw = raw[:-1]
    else:
        raw = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if raw != confirm:
            raise SystemExit("Passwords do not match.")
    if len(raw) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(raw) > MAX_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    return raw


async def _open_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return factory, engine


async def bootstrap_user(
    *,
    username: str,
    display_name: str,
    email: str,
    password: str,
) -> None:
    factory, engine = await _open_db()
    try:
        async with factory() as session:
            users = UserRepository(session)
            if await users.get_by_username(username) is not None:
                raise SystemExit(f"username already exists: {username}")
            user = await users.create(
                username=username,
                display_name=display_name,
                email=email,
                status=UserStatus.ACTIVE,
            )
            await users.set_password_hash(user.id, hash_password(password))
            await session.commit()
            print(f"bootstrap-user ok user_id={user.id} username={username}")
    finally:
        await engine.dispose()


async def set_password(*, username: str, password: str) -> None:
    factory, engine = await _open_db()
    try:
        async with factory() as session:
            users = UserRepository(session)
            user = await users.get_by_username(username)
            if user is None:
                raise SystemExit(f"user not found: {username}")
            await users.set_password_hash(user.id, hash_password(password))
            revoked = await SessionRepository(session).revoke_all_for_user(user.id)
            await session.commit()
            print(
                f"set-password ok username={username} revoked_sessions={revoked}"
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth.bootstrap")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("bootstrap-user", help="Create ACTIVE user with password")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--password-file", default=None)

    setpw = sub.add_parser("set-password", help="Set password for existing user")
    setpw.add_argument("--username", required=True)
    setpw.add_argument("--password-file", default=None)

    args = parser.parse_args(argv)
    password = _read_password(password_file=args.password_file)

    if args.command == "bootstrap-user":
        asyncio.run(
            bootstrap_user(
                username=args.username.strip(),
                display_name=args.display_name.strip(),
                email=args.email.strip(),
                password=password,
            )
        )
        return 0
    if args.command == "set-password":
        asyncio.run(set_password(username=args.username.strip(), password=password))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
