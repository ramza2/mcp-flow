#!/usr/bin/env python3
"""Isolated stack for Playwright E2E-001 (docs/09).

Boots throwaway Redis + Mock ModelOps + Mock MCP + API + outbox + Celery worker,
seeds one ACTIVE/PUBLISHED Agent + READ_ONLY Tool, writes state JSON.

Never targets ops Redis/containers. Requires TEST_DATABASE_URL /
MCPFLOW_DATABASE_URL pointing at the local test Postgres.

Usage:
  E2E001_ALLOW_DB_RESET=1 python3 scripts/e2e001_isolated_stack.py
"""

from __future__ import annotations

import hashlib
import json
import os
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
MCP_RESULT_TEXT = "e2e001-mock-mcp-sunny"

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
                    # AgentRunAnswerComposer expects {"answer": "..."}.
                    content = {
                        "answer": (
                            f"서울 날씨는 {MCP_RESULT_TEXT}이며 기온은 21도입니다."
                        ),
                    }
                elif (
                    "tool_version_id" in blob
                    or "llm_fit_score" in blob
                    or "candidates" in blob
                ):
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
            await UserRepository(session).set_password_hash(
                user.id, hash_password(PASSWORD)
            )
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


def _reset_db() -> None:
    if os.environ.get("E2E001_ALLOW_DB_RESET", "").strip() != "1":
        raise SystemExit(
            "Refusing to reset DB without E2E001_ALLOW_DB_RESET=1 "
            "(isolates Playwright E2E-001 from ops DBs)."
        )
    from tests.integration.conftest import recover_integration_database

    db_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("MCPFLOW_DATABASE_URL")
    if not db_url:
        raise SystemExit("TEST_DATABASE_URL or MCPFLOW_DATABASE_URL required")
    os.environ["MCPFLOW_DATABASE_URL"] = db_url
    os.environ["TEST_DATABASE_URL"] = db_url
    recover_integration_database(db_url)


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _reset_db()

    modelops_port = _free_port()
    mcp_port = _free_port()
    api_port = _free_port()
    modelops = _start_modelops(modelops_port)
    mcp = _start_mock_mcp(mcp_port)
    modelops_base = f"http://127.0.0.1:{modelops_port}/v1"
    mcp_endpoint = f"http://127.0.0.1:{mcp_port}/mcp"

    redis_url, redis_pid = _start_dedicated_redis()
    os.environ["MCPFLOW_REDIS_URL"] = redis_url
    os.environ["MCPFLOW_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    # Faster outbox poll for the browser wait budget.
    os.environ["MCPFLOW_OUTBOX_POLL_INTERVAL_SECONDS"] = "0.5"

    from app.core.config import get_settings

    get_settings.cache_clear()

    import asyncio

    seeded = asyncio.run(
        _seed(modelops_base=modelops_base, mcp_endpoint=mcp_endpoint)
    )

    children: list[subprocess.Popen[Any]] = []
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
        "api_base_url": f"http://127.0.0.1:{api_port}",
        "api_proxy_target": f"http://127.0.0.1:{api_port}",
        "redis_url": redis_url,
        "modelops_base_url": modelops_base,
        "mcp_endpoint_url": mcp_endpoint,
        "username": USERNAME,
        "password": PASSWORD,
        "agent_name": AGENT_NAME,
        "agent_id": seeded["agent_id"],
        "nl_request": NL_REQUEST,
        "expected_result_fragment": MCP_RESULT_TEXT,
        "frontend_hint": "Set MCPFLOW_API_PROXY_TARGET to api_proxy_target and run Vite.",
        **seeded,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"E2E001_READY state={STATE_PATH}", flush=True)
    print(json.dumps({"api_proxy_target": state["api_proxy_target"]}, ensure_ascii=False), flush=True)

    stop = threading.Event()

    def _handle(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        while not stop.is_set():
            time.sleep(0.2)
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(
                        f"child exited early pid={child.pid} code={child.returncode}"
                    )
    finally:
        for child in children:
            child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        _stop_pidfile(redis_pid)
        modelops.shutdown()
        mcp.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
