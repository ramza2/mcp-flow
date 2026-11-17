#!/usr/bin/env python3
"""Isolated stack for Playwright E2E-001 (docs/09).

Boots throwaway Redis + owned disposable Postgres + Mock ModelOps/MCP + API +
outbox + Celery worker, seeds one ACTIVE/PUBLISHED Agent + READ_ONLY Tool,
writes state JSON.

Never targets ops Redis/containers. Never TRUNCATE / reset a shared or
pre-existing Postgres (localhost+*_test alone is not enough). DB work runs
only on a cluster the harness creates under STATE_DIR.

Requires explicit ``E2E001_ALLOW_DB_RESET=1`` to create/destroy that owned
throwaway Postgres.

Usage:
  E2E001_ALLOW_DB_RESET=1 python3 scripts/e2e001_isolated_stack.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import which
from typing import Any

# Ensure backend package imports resolve when run as a script.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

STATE_DIR = Path(os.environ.get("E2E001_STATE_DIR", "/tmp/mcpflow-e2e001"))
STATE_PATH = STATE_DIR / "state.json"
QUERY_TOKEN = "E2E001_UNIQUE_WEATHER_TOKEN"
NL_REQUEST = f"서울 날씨 {QUERY_TOKEN} 알려줘"
USERNAME = "e2e001_user"
PASSWORD = "e2e001-pass-word"
AGENT_NAME = "E2E001 Weather Agent"
# Per-boot nonce so a stale UI/state cannot satisfy the scenario.
RUN_NONCE = uuid.uuid4().hex[:12]
MCP_RESULT_TEXT = f"e2e001-mock-mcp-sunny-{RUN_NONCE}"
_OWNED_PG_USER = "e2e001"
_OWNED_PG_DB = "e2e001_owned_test"

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
        "condition": {"type": "string"},
        "temperature_c": {"type": "number"},
    },
    "required": ["location", "condition", "temperature_c"],
    "additionalProperties": False,
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_tcp(host: str, port: int, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"TCP {host}:{port} not ready")


class _Shared:
    tool_version_id: str = ""
    mcp_calls: int = 0
    lock = threading.Lock()


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def _secure_unlink(path: Path) -> None:
    """Overwrite then delete a file that may hold credentials."""
    if not path.is_file():
        return
    try:
        size = max(path.stat().st_size, 1)
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def _scrub_sensitive_state() -> None:
    for name in ("state.json", "cj", "cj2", "cj3", "cj4"):
        _secure_unlink(STATE_DIR / name)


def _write_state_file(payload: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(STATE_PATH, 0o600)


def _pg_bin(name: str) -> str:
    for version in ("16", "15", "14"):
        candidate = Path(f"/usr/lib/postgresql/{version}/bin") / name
        if candidate.is_file():
            return str(candidate)
    found = which(name)
    if found:
        return found
    raise RuntimeError(f"{name} required for owned disposable Postgres")


def _compose_answer_from_messages(messages: list[Any]) -> dict[str, str]:
    """Ground answer text in schema evidence (not a fixed canned string alone)."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        raw = message.get("content")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            continue
        condition = evidence.get("condition")
        if not isinstance(condition, str) or not condition.strip():
            continue
        location = evidence.get("location")
        loc = location if isinstance(location, str) and location.strip() else "조회 위치"
        temperature = evidence.get("temperature_c")
        if isinstance(temperature, (int, float)):
            return {
                "answer": (f"{loc} 날씨는 {condition}이며 기온은 {temperature}도입니다."),
            }
        return {"answer": f"{loc} 날씨는 {condition}입니다."}
    return {"answer": "표시할 검증된 조회 결과가 없습니다."}


def _start_dedicated_postgres() -> tuple[str, Path]:
    """Create a throwaway Postgres cluster owned by this harness under STATE_DIR."""
    data_dir = STATE_DIR / f"pg-{uuid.uuid4().hex}"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    initdb = _pg_bin("initdb")
    pg_ctl = _pg_bin("pg_ctl")
    createdb = _pg_bin("createdb")
    init = subprocess.run(
        [
            initdb,
            "-D",
            str(data_dir),
            "-U",
            _OWNED_PG_USER,
            "--auth-local=trust",
            "--auth-host=trust",
            "--no-instructions",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        raise RuntimeError(f"initdb failed: {init.stderr or init.stdout}")
    with (data_dir / "postgresql.conf").open("a", encoding="utf-8") as conf:
        conf.write("\nlisten_addresses = '127.0.0.1'\n")
        conf.write(f"unix_socket_directories = '{data_dir}'\n")
        conf.write("fsync = off\n")
        conf.write("full_page_writes = off\n")
        conf.write("synchronous_commit = off\n")
    port = _free_port()
    start = subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-o",
            f"-p {port} -k {data_dir}",
            "-l",
            str(data_dir / "pg.log"),
            "start",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        log_tail = ""
        log_path = data_dir / "pg.log"
        if log_path.is_file():
            log_tail = log_path.read_text(encoding="utf-8")[-2000:]
        raise RuntimeError(f"pg_ctl start failed: {start.stderr or start.stdout}\n{log_tail}")
    _wait_tcp("127.0.0.1", port, timeout_s=15)
    created = subprocess.run(
        [
            createdb,
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            _OWNED_PG_USER,
            _OWNED_PG_DB,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        raise RuntimeError(f"createdb failed: {created.stderr or created.stdout}")
    url = f"postgresql+asyncpg://{_OWNED_PG_USER}@127.0.0.1:{port}/{_OWNED_PG_DB}"
    return url, data_dir


def _stop_dedicated_postgres(data_dir: Path | None) -> None:
    if data_dir is None or not data_dir.is_dir():
        return
    try:
        pg_ctl = _pg_bin("pg_ctl")
    except RuntimeError:
        shutil.rmtree(data_dir, ignore_errors=True)
        return
    subprocess.run(
        [pg_ctl, "-D", str(data_dir), "-m", "fast", "stop"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(data_dir, ignore_errors=True)


def _migrate_owned_database(db_url: str) -> None:
    """Alembic upgrade on the owned empty DB only — never TRUNCATE shared DBs."""
    from tests.integration.conftest import alembic_upgrade_head_sync

    alembic_upgrade_head_sync(db_url)


def _prepare_owned_database() -> tuple[str, Path]:
    if os.environ.get("E2E001_ALLOW_DB_RESET", "").strip() != "1":
        raise SystemExit(
            "Refusing to create disposable Postgres without explicit "
            "E2E001_ALLOW_DB_RESET=1 (no default approval; shared DBs must "
            "not be wiped)."
        )
    # External URLs are ignored for reset — localhost+*_test is not ownership.
    db_url, data_dir = _start_dedicated_postgres()
    os.environ["MCPFLOW_DATABASE_URL"] = db_url
    os.environ["TEST_DATABASE_URL"] = db_url
    try:
        _migrate_owned_database(db_url)
    except Exception:
        _stop_dedicated_postgres(data_dir)
        raise
    return db_url, data_dir


def _start_modelops(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8") or "{}")
            path = self.path.split("?", 1)[0]
            if path.endswith("/embeddings"):
                inputs = body.get("input") or []
                if isinstance(inputs, str):
                    inputs = [inputs]
                data = []
                for i, text in enumerate(inputs):
                    if QUERY_TOKEN in str(text) or str(text) == NL_REQUEST:
                        vector = [1.0, 0.0, 0.0, 0.0]
                    else:
                        digest = hashlib.sha256(str(text).encode()).digest()
                        vector = [((digest[j] / 255.0) * 2 - 1) for j in range(4)]
                    data.append({"index": i, "embedding": vector, "object": "embedding"})
                payload = {"object": "list", "data": data, "model": "emb"}
            elif path.endswith("/chat/completions"):
                messages = body.get("messages") or []
                blob = json.dumps(messages, ensure_ascii=False)
                if (
                    "final response composer" in blob
                    or '"evidence"' in blob
                    or "verified JSON evidence" in blob
                ):
                    # Echo schema evidence from chat-result (MCP-backed).
                    content = _compose_answer_from_messages(
                        messages if isinstance(messages, list) else []
                    )
                elif "tool_version_id" in blob or "llm_fit_score" in blob or "candidates" in blob:
                    content = {
                        "candidates": [
                            {
                                "tool_version_id": _Shared.tool_version_id,
                                "llm_fit_score": 0.95,
                                "reason_summary": "e2e001 weather match",
                            }
                        ],
                        "ambiguities": [],
                    }
                else:
                    content = {
                        "intent": "날씨 조회",
                        "entities": [
                            {
                                "name": "location",
                                "value": "서울",
                                "source": "USER_EXPLICIT",
                            }
                        ],
                        "constraints": [],
                        "expected_outputs": ["날씨"],
                        "required_capabilities": ["weather.lookup"],
                        "risk_hints": ["READ_ONLY"],
                        "missing_inputs": [],
                        "ambiguities": [],
                        "needs_clarification": False,
                    }
                payload = {
                    "id": "chatcmpl-e2e001",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(content, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _start_mock_mcp(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/__e2e001/stats":
                self.send_response(404)
                self.end_headers()
                return
            with _Shared.lock:
                payload = {
                    "tools_call_count": _Shared.mcp_calls,
                    "expected_result_fragment": MCP_RESULT_TEXT,
                    "run_nonce": RUN_NONCE,
                }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8") or "{}")
            method = body.get("method")
            with _Shared.lock:
                if method == "tools/call":
                    _Shared.mcp_calls += 1
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": MCP_RESULT_TEXT}],
                    "structuredContent": {
                        "location": "서울",
                        "condition": MCP_RESULT_TEXT,
                        "temperature_c": 21,
                    },
                    "isError": False,
                },
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _start_dedicated_redis() -> tuple[str, Path]:
    if which("redis-server") is None:
        raise RuntimeError("redis-server required for E2E-001 isolated broker")
    run_dir = STATE_DIR / f"redis-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_path = run_dir / "redis.pid"
    port = _free_port()
    subprocess.Popen(
        [
            "redis-server",
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--dir",
            str(run_dir),
            "--pidfile",
            str(pid_path),
            "--logfile",
            str(run_dir / "redis.log"),
            "--save",
            "",
            "--appendonly",
            "no",
            "--daemonize",
            "yes",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).wait(timeout=10)
    _wait_tcp("127.0.0.1", port, timeout_s=5)
    return f"redis://127.0.0.1:{port}/0", pid_path


def _stop_pidfile(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_path.unlink(missing_ok=True)


async def _seed(
    *,
    modelops_base: str,
    mcp_endpoint: str,
) -> dict[str, Any]:
    from app.auth.passwords import hash_password
    from app.domain.enums import (
        AgentToolGrantEffect,
        MCPServerStatus,
        MCPToolStatus,
        ResourceGrantResourceType,
        RiskClass,
        ToolVersionValidationStatus,
        UserStatus,
    )
    from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
    from app.repositories.agent import AgentRepository
    from app.repositories.agent_tool_grant import AgentToolGrantRepository
    from app.repositories.agent_version import AgentVersionRepository
    from app.repositories.embedding_profile import EmbeddingProfileRepository
    from app.repositories.llm_profile import LLMProfileRepository
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.repositories.role import PermissionRepository
    from app.repositories.tool_embedding import ToolEmbeddingRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        ResourceGrantCreate,
        RoleCreate,
        RolePermissionReplaceRequest,
        UserCreate,
        UserRoleReplaceRequest,
    )
    from app.services.authorization import ResourceGrantService
    from app.services.embedding_profile import EmbeddingProfileService
    from app.services.role import RoleService
    from app.services.user import UserService
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    db_url = os.environ["MCPFLOW_DATABASE_URL"]
    engine = create_async_engine(db_url, pool_pre_ping=True)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        async with factory() as session:
            user_svc = UserService(session)
            user = await user_svc.create(
                UserCreate(
                    username=USERNAME,
                    display_name="E2E001 User",
                    email=f"{USERNAME}@example.com",
                    status=UserStatus.ACTIVE,
                )
            )
            await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
            await session.refresh(user)
            role = await RoleService(session).create(
                RoleCreate(code=f"e2e001-r-{uuid.uuid4().hex[:8]}", name="E2E001")
            )
            perm_repo = PermissionRepository(session)
            codes = ("mcp.tool.execute", "agent.read", "execution.read")
            perm_ids = []
            for code in codes:
                row = await perm_repo.get_by_code(code)
                if row is None:
                    raise RuntimeError(f"missing permission seed: {code}")
                perm_ids.append(row.id)
            await RoleService(session).replace_permissions(
                role.id,
                RolePermissionReplaceRequest(permission_ids=perm_ids),
                expected_lock_version=int(role.lock_version),
            )
            await UserService(session).replace_roles(
                user.id,
                UserRoleReplaceRequest(role_ids=[role.id]),
                expected_lock_version=int(user.lock_version),
            )

            emb = await EmbeddingProfileRepository(session).create(
                code=f"e2e001-emb-{uuid.uuid4().hex[:8]}",
                name="E2E001 Emb",
                provider=OPENAI_COMPATIBLE_PROVIDER,
                model="emb",
                base_url=modelops_base,
                dimension=4,
                distance_metric="cosine",
                is_active_for_tools=False,
            )
            emb = await EmbeddingProfileService(session).activate_for_tools(
                emb.id, expected_lock_version=int(emb.lock_version)
            )
            llm = await LLMProfileRepository(session).create(
                code=f"e2e001-llm-{uuid.uuid4().hex[:8]}",
                name="E2E001 LLM",
                provider=OPENAI_COMPATIBLE_PROVIDER,
                model="gpt-test",
                base_url=modelops_base,
                parameters={"temperature": 0.0},
            )
            server = await MCPServerRepository(session).create(
                code=f"e2e001-srv-{uuid.uuid4().hex[:8]}",
                name="E2E001 MCP",
                transport_type="STREAMABLE_HTTP",
                endpoint_url=mcp_endpoint,
                status=MCPServerStatus.ACTIVE.value,
                auth_type="NONE",
            )
            tools = MCPToolRepository(session)
            tool = await tools.create_tool(
                mcp_server_id=server.id,
                remote_name="e2e001_weather",
                display_name="E2E001 Weather",
                tags=["weather"],
                status=MCPToolStatus.ACTIVE.value,
            )
            version = await tools.create_version(
                mcp_tool_id=tool.id,
                version_no=1,
                content_hash=hashlib.sha256(QUERY_TOKEN.encode()).hexdigest(),
                validation_status=ToolVersionValidationStatus.VALID.value,
                remote_description=NL_REQUEST,
                input_schema={
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                    "additionalProperties": False,
                },
                output_schema=_OUTPUT_SCHEMA,
            )
            tool.current_version_id = version.id
            await MCPToolPolicyRepository(session).create(
                mcp_tool_id=tool.id,
                risk_class=RiskClass.READ_ONLY.value,
                requires_confirmation=False,
                requires_approval=False,
                approval_policy_id=None,
                timeout_ms=30_000,
                max_attempts=1,
                backoff_policy=None,
                max_result_bytes=65_536,
                allow_auto_select=True,
                data_classification=None,
                policy_metadata=None,
            )
            await ResourceGrantService(session).create_for_user(
                user.id,
                ResourceGrantCreate(
                    resource_type=ResourceGrantResourceType.MCP_TOOL,
                    resource_id=tool.id,
                ),
            )
            agent = await AgentRepository(session).create(
                code=f"e2e001-agt-{uuid.uuid4().hex[:8]}",
                name=AGENT_NAME,
                status="ACTIVE",
                visibility="INTERNAL",
                owner_id=user.id,
            )
            agent_version = await AgentVersionRepository(session).create(
                agent_id=agent.id,
                version_no=1,
                system_instruction="Structure the request; select weather tools carefully.",
                llm_profile_id=llm.id,
                request_schema_version="1.0",
                plan_schema_version="1.0",
                selection_settings={
                    "auto_select_threshold": 0.82,
                    "confirmation_threshold": 0.60,
                    "max_candidates": 12,
                },
                planning_settings={},
                response_settings={},
                content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            )
            agent_version.status = "PUBLISHED"
            agent.current_version_id = agent_version.id
            await AgentToolGrantRepository(session).replace_all(
                agent_version.id,
                [
                    {
                        "mcp_tool_id": tool.id,
                        "effect": AgentToolGrantEffect.ALLOW.value,
                    }
                ],
            )
            await ToolEmbeddingRepository(session).upsert_ready_if_current(
                tool_version_id=version.id,
                embedding_profile_id=emb.id,
                search_text=NL_REQUEST,
                content_hash=hashlib.sha256(NL_REQUEST.encode()).hexdigest(),
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
            await session.commit()
            _Shared.tool_version_id = str(version.id)
            return {
                "user_id": str(user.id),
                "agent_id": str(agent.id),
                "agent_version_id": str(agent_version.id),
                "tool_id": str(tool.id),
                "tool_version_id": str(version.id),
            }
    finally:
        await engine.dispose()


def main() -> int:
    _ensure_state_dir()
    # Drop credentials / prior run markers before bootstrapping a new stack.
    _scrub_sensitive_state()

    redis_pid: Path | None = None
    pg_data: Path | None = None
    modelops: ThreadingHTTPServer | None = None
    mcp: ThreadingHTTPServer | None = None
    children: list[subprocess.Popen[Any]] = []
    exit_code = 1

    try:
        _db_url, pg_data = _prepare_owned_database()

        modelops_port = _free_port()
        mcp_port = _free_port()
        api_port = _free_port()
        modelops = _start_modelops(modelops_port)
        mcp = _start_mock_mcp(mcp_port)
        modelops_base = f"http://127.0.0.1:{modelops_port}/v1"
        mcp_endpoint = f"http://127.0.0.1:{mcp_port}/mcp"
        mcp_stats_url = f"http://127.0.0.1:{mcp_port}/__e2e001/stats"

        redis_url, redis_pid = _start_dedicated_redis()
        os.environ["MCPFLOW_REDIS_URL"] = redis_url
        # Faster outbox poll for the browser wait budget.
        os.environ["MCPFLOW_OUTBOX_POLL_INTERVAL_SECONDS"] = "0.5"

        from app.core.config import get_settings

        get_settings.cache_clear()

        import asyncio

        seeded = asyncio.run(_seed(modelops_base=modelops_base, mcp_endpoint=mcp_endpoint))

        env = os.environ.copy()
        env["MCPFLOW_DATABASE_URL"] = os.environ["MCPFLOW_DATABASE_URL"]
        env["MCPFLOW_REDIS_URL"] = redis_url
        env["MCPFLOW_OUTBOX_POLL_INTERVAL_SECONDS"] = "0.5"
        env["PYTHONPATH"] = str(_BACKEND_ROOT)

        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            cwd=str(_BACKEND_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(STATE_DIR / "api.log", "w", encoding="utf-8"),
        )
        children.append(api)

        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.infrastructure.celery_app:celery_app",
                "worker",
                "-Q",
                "execution",
                "--pool=solo",
                "--concurrency=1",
                "--loglevel=WARNING",
                "-n",
                "e2e001@%h",
            ],
            cwd=str(_BACKEND_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(STATE_DIR / "worker.log", "w", encoding="utf-8"),
        )
        children.append(worker)

        outbox = subprocess.Popen(
            [sys.executable, "-m", "app.entrypoints.outbox"],
            cwd=str(_BACKEND_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(STATE_DIR / "outbox.log", "w", encoding="utf-8"),
        )
        children.append(outbox)

        _wait_tcp("127.0.0.1", api_port, timeout_s=30)

        state = {
            "run_nonce": RUN_NONCE,
            "api_base_url": f"http://127.0.0.1:{api_port}",
            "api_proxy_target": f"http://127.0.0.1:{api_port}",
            "database_owned": True,
            "database_name": _OWNED_PG_DB,
            "redis_url": redis_url,
            "modelops_base_url": modelops_base,
            "mcp_endpoint_url": mcp_endpoint,
            "mcp_stats_url": mcp_stats_url,
            "username": USERNAME,
            "password": PASSWORD,
            "agent_name": AGENT_NAME,
            "agent_id": seeded["agent_id"],
            "nl_request": NL_REQUEST,
            "expected_result_fragment": MCP_RESULT_TEXT,
            "frontend_hint": ("Set MCPFLOW_API_PROXY_TARGET to api_proxy_target and run Vite."),
            **seeded,
        }
        _write_state_file(state)
        print(f"E2E001_READY state={STATE_PATH}", flush=True)
        print(
            json.dumps(
                {"api_proxy_target": state["api_proxy_target"]},
                ensure_ascii=False,
            ),
            flush=True,
        )

        stop = threading.Event()

        def _handle(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        while not stop.is_set():
            time.sleep(0.2)
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(
                        f"child exited early pid={child.pid} code={child.returncode}"
                    )
        exit_code = 0
    finally:
        for child in children:
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        if redis_pid is not None:
            _stop_pidfile(redis_pid)
        if modelops is not None:
            modelops.shutdown()
        if mcp is not None:
            mcp.shutdown()
        _stop_dedicated_postgres(pg_data)
        _scrub_sensitive_state()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
