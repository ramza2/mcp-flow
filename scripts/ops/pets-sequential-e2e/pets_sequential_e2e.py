#!/usr/bin/env python3
"""Idempotent ops helper: Factory pets list+detail → Agent sequential pair E2E.

Uses only the public /api/v1 surface (login + CSRF). Never touches DB SQL,
never mutates existing Factory Import / List pets ToolVersions, never bypasses
Review → Publish → Runtime Binding → Connection Test → Discovery → Activate.

Stdlib only so it can run on the ops host without installing backend deps.

Commands:
  dry-run   Print planned HTTP sequence (no network).
  setup     OpenAPI → activate tools → new Agent grants/policy (idempotent).
  verify    NL Agent Run expecting tool_1 list + tool_2 detail → Alpha.
  status    Show local state file.

Environment:
  MCPFLOW_BASE_URL          default http://127.0.0.1
  MCPFLOW_USERNAME          required for setup/verify
  MCPFLOW_PASSWORD          required for setup/verify (never logged)
  MCPFLOW_LLM_PROFILE_ID    required for setup (existing LLM profile UUID)
  MCPFLOW_OPENAPI_PATH      default /tmp/mcpflow-pets-e2e-openapi.yaml
                            (falls back to bundled openapi/pets-sequential-e2e.yaml)
  MCPFLOW_STATE_PATH        default ./pets-sequential-e2e.state.json
  MCPFLOW_RUN_ID            default pets-seq-e2e-v1 (idempotency namespace)
  MCPFLOW_MOCK_BASE_URL     default http://factory-mock-upstream:8091
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_OPENAPI = SCRIPT_DIR / "openapi" / "pets-sequential-e2e.yaml"
DEFAULT_OPENAPI = Path("/tmp/mcpflow-pets-e2e-openapi.yaml")
DEFAULT_STATE = Path("pets-sequential-e2e.state.json")
DEFAULT_MOCK = "http://factory-mock-upstream:8091"
DEFAULT_BASE = "http://127.0.0.1"
LIST_OP = "GET /pets"
DETAIL_OP = "GET /pets/{pet_id}"
NL_REQUEST = "첫 번째 반려동물의 상세 정보를 조회해 줘"
AGENT_NAME = "Pets Sequential E2E"


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + "/"
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: set[int] | None = None,
    ) -> Any:
        hdrs = dict(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and self.csrf:
            hdrs.setdefault("X-CSRF-Token", self.csrf)
        req = Request(self._url(path), data=body, headers=hdrs, method=method.upper())
        try:
            with self.opener.open(req, timeout=120) as resp:
                raw = resp.read()
                status = resp.getcode() or 200
                ctype = resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            status = int(exc.code)
            ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
            payload: Any
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", errors="replace")
            if expect is not None and status in expect:
                return {"_status": status, "_body": payload}
            raise ApiError(
                f"{method} {path} → HTTP {status}: {payload}",
                status=status,
                body=payload,
            ) from exc
        except URLError as exc:
            raise ApiError(f"{method} {path} network error: {exc}") from exc

        if expect is not None and status not in expect:
            raise ApiError(f"{method} {path} → unexpected HTTP {status}", status=status)
        if not raw:
            return {"_status": status}
        if "application/json" in ctype or raw[:1] in (b"{", b"["):
            return json.loads(raw.decode("utf-8"))
        return raw

    def login(self, username: str, password: str) -> dict[str, Any]:
        session = self.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": username, "password": password},
            expect={200},
        )
        csrf = self.request("GET", "/api/v1/auth/csrf", expect={200})
        self.csrf = csrf["csrf_token"]
        return session

    def multipart_job(self, openapi_path: Path, idem_key: str) -> dict[str, Any]:
        boundary = f"----mcpflow{uuid.uuid4().hex}"
        filename = openapi_path.name
        content = openapi_path.read_bytes()
        parts = [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="source"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        return self.request(
            "POST",
            "/api/v1/factory/jobs",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Idempotency-Key": idem_key,
            },
            expect={200, 201},
        )


def _log(msg: str) -> None:
    print(msg, flush=True)


# Never persist credentials / session material in the local state file.
_STATE_FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "mcpflow_password",
        "csrf",
        "csrf_token",
        "session",
        "session_token",
        "raw_session_token",
        "cookie",
        "cookies",
        "authorization",
        "auth_header",
    }
)


def _scrub_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy without credential/session keys (defense in depth)."""
    return {
        key: value
        for key, value in state.items()
        if str(key).lower() not in _STATE_FORBIDDEN_KEYS
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace state JSON as mode 0600 (no secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = _scrub_state(state)
    payload = json.dumps(cleaned, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _require_clean_discovery(payload: dict[str, Any], *, phase: str) -> None:
    """Fail closed unless success=true and factory_diff.status=CLEAN."""
    if not isinstance(payload, dict):
        raise ApiError(f"Discovery {phase}: response is not an object: {payload!r}")
    if payload.get("success") is not True:
        raise ApiError(
            f"Discovery {phase}: success must be true "
            f"(got success={payload.get('success')!r}): {payload}"
        )
    factory_diff = payload.get("factory_diff")
    if not isinstance(factory_diff, dict):
        raise ApiError(
            f"Discovery {phase}: factory_diff missing or invalid "
            f"(got {factory_diff!r}): {payload}"
        )
    status = factory_diff.get("status")
    if status != "CLEAN":
        raise ApiError(
            f"Discovery {phase}: factory_diff.status must be CLEAN "
            f"(got {status!r}; MISMATCHED/AMBIGUOUS/missing fail-closed): {payload}"
        )


def _idem(run_id: str, step: str) -> str:
    # Stable per run_id+step so re-runs replay Factory idempotent creates.
    return f"{run_id}:{step}"[:128]


def _resolve_openapi(cli_path: str | None) -> Path:
    if cli_path:
        path = Path(cli_path)
    else:
        env = os.environ.get("MCPFLOW_OPENAPI_PATH")
        path = Path(env) if env else DEFAULT_OPENAPI
    if path.is_file():
        return path
    if BUNDLED_OPENAPI.is_file():
        _log(f"NOTE: {path} missing; using bundled {BUNDLED_OPENAPI}")
        return BUNDLED_OPENAPI
    raise SystemExit(f"OpenAPI file not found: {path}")


def planned_steps() -> list[str]:
    return [
        "POST /auth/login + GET /auth/csrf",
        "POST /factory/jobs (multipart OpenAPI, Idempotency-Key)",
        "poll GET /factory/jobs/{id} → SUCCEEDED",
        "GET /factory/jobs/{id}/candidates → pick GET /pets + GET /pets/{pet_id}",
        "POST /factory/jobs/{id}/builds (both candidates)",
        "POST /factory/builds/{id}/generate",
        "POST /factory/artifacts/{id}/tests + poll PASS",
        "POST /factory/test-runs/{id}/review decision=APPROVE",
        "POST /factory/reviews/{id}/publish → NEW mcp_server_id / tool ids",
        "POST /factory/publishes/{id}/runtime-bindings",
        "PATCH runtime-binding base_url=factory-mock-upstream:8091 auth_type=NONE",
        "POST runtime-binding/ready → mcp_endpoint_url",
        "PATCH /mcp/servers/{id} endpoint_url=<mcp_endpoint_url>",
        "POST /mcp/servers/{id}/connection-tests → SUCCEEDED",
        "POST discoveries preview+apply (success=true AND factory_diff.status=CLEAN)",
        "POST /mcp/servers/{id}/activate",
        "POST /mcp/tools/{id}/activate (both; If-Match)",
        "PUT /mcp/tools/{id}/policy READ_ONLY allow_auto_select (both)",
        "POST /users/{me}/resource-grants MCP_TOOL (both)",
        "POST /agents (new Pets Sequential E2E) + version + tool-grants + validate/publish",
        "PATCH agent status=ACTIVE",
        "wait until ToolSelector retrieval can see both (embedding READY via outbox)",
        "verify: Agent Run NL → select list primary → 2-step plan → Alpha",
    ]


def cmd_dry_run(_: argparse.Namespace) -> int:
    _log("Pets Sequential E2E — planned sequence (no network)")
    for i, step in enumerate(planned_steps(), 1):
        _log(f"  {i:02d}. {step}")
    _log("")
    _log("Isolation rules:")
    _log("  - New Factory Publish → new MCP Server (fac-pub-*).")
    _log("  - Never PATCH the old List pets server / ToolVersions.")
    _log("  - New Agent 'Pets Sequential E2E' grants only new tool UUIDs.")
    _log("  - No DB INSERT/UPDATE; no auth bypass; no Factory Mock UI.")
    _log(f"  - Runtime base_url default: {DEFAULT_MOCK}")
    _log(f"  - NL verify prompt: {NL_REQUEST!r}")
    return 0


def _poll_job(client: Client, job_id: str, *, timeout_s: float = 300.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = client.request("GET", f"/api/v1/factory/jobs/{job_id}", expect={200})
        status = job.get("status")
        if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return job
        time.sleep(2.0)
    raise ApiError(f"Factory job {job_id} did not finish within {timeout_s}s")


def _poll_test_run(
    client: Client, test_run_id: str, *, timeout_s: float = 300.0
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = client.request(
            "GET", f"/api/v1/factory/test-runs/{test_run_id}", expect={200}
        )
        if row.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return row
        time.sleep(2.0)
    raise ApiError(f"TestRun {test_run_id} did not finish within {timeout_s}s")


def _pick_candidates(items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    by_key = {str(c.get("operation_key")): c for c in items}
    if LIST_OP not in by_key or DETAIL_OP not in by_key:
        keys = sorted(by_key)
        raise ApiError(
            f"Expected candidates {LIST_OP!r} and {DETAIL_OP!r}; got {keys}"
        )
    list_c = by_key[LIST_OP]
    detail_c = by_key[DETAIL_OP]
    list_schema = list_c.get("response_schema")
    if not isinstance(list_schema, dict):
        raise ApiError("GET /pets response_schema missing — cannot sequential-pair")
    pets = (list_schema.get("properties") or {}).get("pets")
    if not isinstance(pets, dict) or pets.get("type") != "array":
        raise ApiError("GET /pets response_schema.pets must be array")
    items_schema = pets.get("items") or {}
    id_schema = ((items_schema.get("properties") or {}).get("id")) if isinstance(
        items_schema, dict
    ) else None
    if not isinstance(id_schema, dict) or id_schema.get("type") != "integer":
        raise ApiError(
            "GET /pets response_schema must expose pets.items.properties.id type=integer"
        )
    params = detail_c.get("parameters") or []
    pet_id = next(
        (p for p in params if isinstance(p, dict) and p.get("name") == "pet_id"),
        None,
    )
    if pet_id is None:
        raise ApiError("GET /pets/{pet_id} must declare path parameter pet_id")
    return list_c, detail_c


def _ensure_policy(client: Client, tool_id: str) -> dict[str, Any]:
    existing = client.request(
        "GET",
        f"/api/v1/mcp/tools/{tool_id}/policy",
        expect={200, 404},
    )
    body = {
        "risk_class": "READ_ONLY",
        "requires_confirmation": False,
        "requires_approval": False,
        "approval_policy_id": None,
        "timeout_ms": 30000,
        "max_attempts": 1,
        "backoff_policy": None,
        "max_result_bytes": 65536,
        "allow_auto_select": True,
        "data_classification": None,
        "policy_metadata": {"e2e": "pets-sequential"},
    }
    if isinstance(existing, dict) and existing.get("_status") != 404 and "id" in existing:
        body["lock_version"] = int(existing["lock_version"])
    return client.request(
        "PUT",
        f"/api/v1/mcp/tools/{tool_id}/policy",
        json_body=body,
        expect={200},
    )


def _iter_user_resource_grants(
    client: Client, user_id: str, *, page_size: int = 100
) -> list[dict[str, Any]]:
    """Page through all user ResourceGrants (page_size capped at 100)."""
    size = max(1, min(int(page_size), 100))
    page = 1
    items: list[dict[str, Any]] = []
    while True:
        listed = client.request(
            "GET",
            f"/api/v1/users/{user_id}/resource-grants?page={page}&page_size={size}",
            expect={200},
        )
        batch = listed.get("items") or []
        if not isinstance(batch, list):
            raise ApiError(f"resource-grants page {page} items invalid: {listed}")
        items.extend(item for item in batch if isinstance(item, dict))
        if not listed.get("has_next"):
            break
        if not batch:
            break
        page += 1
        if page > 10_000:
            raise ApiError("resource-grants pagination exceeded safety limit")
    return items


def _ensure_resource_grant(client: Client, user_id: str, tool_id: str) -> None:
    for item in _iter_user_resource_grants(client, user_id):
        if (
            item.get("resource_type") == "MCP_TOOL"
            and str(item.get("resource_id")) == tool_id
        ):
            return
    try:
        client.request(
            "POST",
            f"/api/v1/users/{user_id}/resource-grants",
            json_body={"resource_type": "MCP_TOOL", "resource_id": tool_id},
            expect={200, 201},
        )
    except ApiError as exc:
        # Race / concurrent create: treat conflict as already granted.
        if exc.status == 409:
            return
        raise


def _wait_embeddings_via_select(
    client: Client,
    *,
    agent_id: str,
    list_version_id: str,
    detail_version_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Embedding READY is not a public HTTP field; probe via ToolSelector."""
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        created = client.request(
            "POST",
            "/api/v1/agent-requests",
            json_body={"agent_id": agent_id, "request_text": NL_REQUEST},
            expect={201},
        )
        request_id = created["id"]
        analyzed = client.request(
            "POST",
            f"/api/v1/agent-requests/{request_id}/analyze",
            expect={200, 502, 409},
        )
        if isinstance(analyzed, dict) and analyzed.get("status") == "RETRIEVING":
            selection = client.request(
                "POST",
                f"/api/v1/agent-requests/{request_id}/select-tool",
                expect={200, 502, 409},
            )
            last = selection
            sel = (selection or {}).get("selection") or {}
            decision = sel.get("decision")
            selected = str(sel.get("selected_tool_version_id") or "")
            if decision == "AUTO_SELECT" and selected == list_version_id:
                return {
                    "request_id": request_id,
                    "selection": sel,
                    "probe": "ready_list_primary",
                }
            if decision == "AUTO_SELECT" and selected == detail_version_id:
                return {
                    "request_id": request_id,
                    "selection": sel,
                    "probe": "detail_selected_directly",
                }
            if decision == "NO_MATCH":
                _log("  embedding/retrieval still NO_MATCH; waiting…")
            else:
                _log(
                    f"  select decision={decision} selected={selected} "
                    f"(want list_version={list_version_id}); waiting…"
                )
        time.sleep(5.0)
    raise ApiError(
        "Timed out waiting for ToolSelector to AUTO_SELECT list primary "
        f"(last={last})"
    )


def cmd_setup(args: argparse.Namespace) -> int:
    username = os.environ.get("MCPFLOW_USERNAME") or args.username
    password = os.environ.get("MCPFLOW_PASSWORD") or args.password
    llm_profile_id = os.environ.get("MCPFLOW_LLM_PROFILE_ID") or args.llm_profile_id
    if not username or not password:
        raise SystemExit("MCPFLOW_USERNAME / MCPFLOW_PASSWORD required for setup")
    if not llm_profile_id:
        raise SystemExit("MCPFLOW_LLM_PROFILE_ID required for setup")

    base = os.environ.get("MCPFLOW_BASE_URL") or args.base_url
    run_id = os.environ.get("MCPFLOW_RUN_ID") or args.run_id
    mock_url = os.environ.get("MCPFLOW_MOCK_BASE_URL") or args.mock_base_url
    state_path = Path(os.environ.get("MCPFLOW_STATE_PATH") or args.state_path)
    openapi = _resolve_openapi(args.openapi)

    state = _load_state(state_path)
    if state.get("run_id") and state["run_id"] != run_id:
        raise SystemExit(
            f"State file run_id={state['run_id']} != MCPFLOW_RUN_ID={run_id}. "
            "Use a new state path or matching run id."
        )
    state["run_id"] = run_id
    state["openapi_path"] = str(openapi)
    state["mock_base_url"] = mock_url

    client = Client(base)
    _log(f"login as {username} @ {base}")
    session = client.login(username, password)
    user_id = str(session["user"]["id"])
    state["user_id"] = user_id

    # --- Factory analyze -------------------------------------------------
    # Resume: existing job_id skips a new analyze (Factory Idempotency-Key
    # still safe if callers force recreate with a new run_id/state path).
    if "job_id" not in state:
        _log(f"factory analyze ← {openapi}")
        job = client.multipart_job(openapi, _idem(run_id, "analyze"))
        state["job_id"] = str(job["id"])
        _save_state(state_path, state)
    else:
        _log(f"resume analyze job_id={state['job_id']}")
    job = _poll_job(client, state["job_id"])
    if job.get("status") != "SUCCEEDED":
        raise ApiError(f"Analyze job failed: {job}")
    state["job_status"] = "SUCCEEDED"
    _save_state(state_path, state)
    _log(f"analyze SUCCEEDED job_id={state['job_id']}")

    candidates = client.request(
        "GET",
        f"/api/v1/factory/jobs/{state['job_id']}/candidates?page=1&page_size=100",
        expect={200},
    )
    if not candidates.get("items"):
        client.request(
            "POST",
            f"/api/v1/factory/jobs/{state['job_id']}/candidates/materialize",
            expect={200, 201},
        )
        candidates = client.request(
            "GET",
            f"/api/v1/factory/jobs/{state['job_id']}/candidates?page=1&page_size=100",
            expect={200},
        )
    list_c, detail_c = _pick_candidates(list(candidates.get("items") or []))
    state["list_candidate_id"] = str(list_c["id"])
    state["detail_candidate_id"] = str(detail_c["id"])
    _save_state(state_path, state)

    # --- Build / package / test / review / publish -----------------------
    if "build_id" not in state:
        build = client.request(
            "POST",
            f"/api/v1/factory/jobs/{state['job_id']}/builds",
            json_body={
                "candidate_ids": [
                    state["list_candidate_id"],
                    state["detail_candidate_id"],
                ]
            },
            headers={"Idempotency-Key": _idem(run_id, "build")},
            expect={200, 201},
        )
        state["build_id"] = str(build["id"])
        _save_state(state_path, state)
    _log(f"build_id={state['build_id']}")

    if "package_id" not in state:
        pkg = client.request(
            "POST",
            f"/api/v1/factory/builds/{state['build_id']}/generate",
            headers={"Idempotency-Key": _idem(run_id, "package")},
            expect={200, 201},
        )
        state["package_id"] = str(pkg["id"])
        _save_state(state_path, state)
    _log(f"package_id={state['package_id']}")

    if "test_run_id" not in state:
        tr = client.request(
            "POST",
            f"/api/v1/factory/artifacts/{state['package_id']}/tests",
            headers={"Idempotency-Key": _idem(run_id, "test")},
            expect={200, 201},
        )
        state["test_run_id"] = str(tr["id"])
        _save_state(state_path, state)
    tr = _poll_test_run(client, state["test_run_id"])
    if tr.get("status") != "SUCCEEDED" or tr.get("result") != "PASS":
        raise ApiError(f"Sandbox test did not PASS: {tr}")
    state["test_result"] = "PASS"
    _save_state(state_path, state)
    _log(f"test_run PASS id={state['test_run_id']}")

    if "review_id" not in state:
        review = client.request(
            "POST",
            f"/api/v1/factory/test-runs/{state['test_run_id']}/review",
            json_body={
                "decision": "APPROVE",
                "comment": "pets sequential e2e approve",
            },
            headers={"Idempotency-Key": _idem(run_id, "review")},
            expect={200, 201},
        )
        state["review_id"] = str(review["id"])
        _save_state(state_path, state)
    _log(f"review_id={state['review_id']}")

    if "publish_id" not in state:
        pub = client.request(
            "POST",
            f"/api/v1/factory/reviews/{state['review_id']}/publish",
            headers={"Idempotency-Key": _idem(run_id, "publish")},
            expect={200, 201},
        )
        state["publish_id"] = str(pub["id"])
        state["mcp_server_id"] = str(pub["mcp_server_id"])
        state["mcp_tool_ids"] = [str(x) for x in pub["mcp_tool_ids"]]
        _save_state(state_path, state)
    _log(
        f"publish_id={state['publish_id']} server={state['mcp_server_id']} "
        f"tools={state['mcp_tool_ids']}"
    )

    # --- Runtime binding -------------------------------------------------
    if "binding_id" not in state:
        try:
            binding = client.request(
                "POST",
                f"/api/v1/factory/publishes/{state['publish_id']}/runtime-bindings",
                expect={201},
            )
        except ApiError as exc:
            # One binding per publish — resume via GET.
            if exc.status not in {409, 400}:
                raise
            binding = client.request(
                "GET",
                f"/api/v1/factory/publishes/{state['publish_id']}/runtime-binding",
                expect={200},
            )
        state["binding_id"] = str(binding["id"])
        state["binding_lock_version"] = int(binding["lock_version"])
        _save_state(state_path, state)

    binding = client.request(
        "GET",
        f"/api/v1/factory/runtime-bindings/{state['binding_id']}",
        expect={200},
    )
    if binding.get("status") != "READY" or binding.get("base_url") != mock_url:
        if binding.get("status") == "READY":
            raise ApiError(
                f"Binding already READY with base_url={binding.get('base_url')}; "
                "refusing to mutate. Use a new run_id for a new package."
            )
        updated = client.request(
            "PATCH",
            f"/api/v1/factory/runtime-bindings/{state['binding_id']}",
            json_body={
                "base_url": mock_url,
                "auth_type": "NONE",
                "lock_version": int(binding["lock_version"]),
            },
            expect={200},
        )
        ready = client.request(
            "POST",
            f"/api/v1/factory/runtime-bindings/{state['binding_id']}/ready",
            json_body={"lock_version": int(updated["lock_version"])},
            expect={200},
        )
        binding = ready
    state["binding_status"] = binding["status"]
    state["mcp_endpoint_url"] = binding.get("mcp_endpoint_url")
    state["binding_lock_version"] = int(binding["lock_version"])
    _save_state(state_path, state)
    if not state["mcp_endpoint_url"]:
        raise ApiError("Runtime binding READY but mcp_endpoint_url missing")
    _log(f"runtime READY endpoint={state['mcp_endpoint_url']}")

    # --- MCP server connection / discovery / activate --------------------
    server = client.request(
        "GET", f"/api/v1/mcp/servers/{state['mcp_server_id']}", expect={200}
    )
    if server.get("endpoint_url") != state["mcp_endpoint_url"]:
        server = client.request(
            "PATCH",
            f"/api/v1/mcp/servers/{state['mcp_server_id']}",
            json_body={
                "endpoint_url": state["mcp_endpoint_url"],
                "lock_version": int(server["lock_version"]),
            },
            expect={200},
        )
    if server.get("status") != "ACTIVE":
        check = client.request(
            "POST",
            f"/api/v1/mcp/servers/{state['mcp_server_id']}/connection-tests",
            expect={201},
        )
        if check.get("status") != "SUCCEEDED":
            raise ApiError(f"Connection test failed: {check}")
        preview = client.request(
            "POST",
            f"/api/v1/mcp/servers/{state['mcp_server_id']}/discoveries",
            json_body={"mode": "FULL", "apply_changes": False},
            expect={201},
        )
        _require_clean_discovery(preview, phase="preview")
        applied = client.request(
            "POST",
            f"/api/v1/mcp/servers/{state['mcp_server_id']}/discoveries",
            json_body={"mode": "FULL", "apply_changes": True},
            expect={201},
        )
        _require_clean_discovery(applied, phase="apply")
        server = client.request(
            "POST",
            f"/api/v1/mcp/servers/{state['mcp_server_id']}/activate",
            expect={200},
        )
    state["server_status"] = server["status"]
    _save_state(state_path, state)
    _log(f"server status={server['status']}")

    # Map tools by remote_name
    tools_by_remote: dict[str, dict[str, Any]] = {}
    for tool_id in state["mcp_tool_ids"]:
        tool = client.request("GET", f"/api/v1/mcp/tools/{tool_id}", expect={200})
        tools_by_remote[str(tool["remote_name"])] = tool
    if LIST_OP not in tools_by_remote or DETAIL_OP not in tools_by_remote:
        raise ApiError(f"Published tools missing ops: {sorted(tools_by_remote)}")

    list_tool = tools_by_remote[LIST_OP]
    detail_tool = tools_by_remote[DETAIL_OP]
    for tool in (list_tool, detail_tool):
        if tool["status"] != "ACTIVE":
            tool = client.request(
                "POST",
                f"/api/v1/mcp/tools/{tool['id']}/activate",
                headers={"If-Match": str(tool["lock_version"])},
                expect={200},
            )
            tools_by_remote[str(tool["remote_name"])] = tool
        versions = client.request(
            "GET",
            f"/api/v1/mcp/tools/{tool['id']}/versions?page=1&page_size=20",
            expect={200},
        )
        current = None
        for ver in versions.get("items") or []:
            if str(ver["id"]) == str(tool.get("current_version_id")):
                current = ver
                break
        if current is None:
            raise ApiError(f"Tool {tool['id']} missing current version")
        if current.get("output_schema") is None and tool["remote_name"] == LIST_OP:
            raise ApiError(
                "List tool output_schema is null — refuse (legacy Import symptom)"
            )
        _ensure_policy(client, str(tool["id"]))
        _ensure_resource_grant(client, user_id, str(tool["id"]))

    list_tool = tools_by_remote[LIST_OP]
    detail_tool = tools_by_remote[DETAIL_OP]
    state["list_tool_id"] = str(list_tool["id"])
    state["detail_tool_id"] = str(detail_tool["id"])
    state["list_tool_version_id"] = str(list_tool["current_version_id"])
    state["detail_tool_version_id"] = str(detail_tool["current_version_id"])
    _save_state(state_path, state)
    _log(
        f"tools ACTIVE list={state['list_tool_id']} "
        f"detail={state['detail_tool_id']}"
    )

    # --- Dedicated Agent (never reuse Factory E2E Agent) -----------------
    if "agent_id" not in state:
        agent = client.request(
            "POST",
            "/api/v1/agents",
            json_body={
                "name": AGENT_NAME,
                "description": "Isolated sequential READ_ONLY pets pair E2E",
                "visibility": "PRIVATE",
            },
            expect={201},
        )
        state["agent_id"] = str(agent["id"])
        state["agent_lock_version"] = int(agent["lock_version"])
        _save_state(state_path, state)
    agent = client.request(
        "GET", f"/api/v1/agents/{state['agent_id']}", expect={200}
    )

    if "agent_version_id" not in state:
        version = client.request(
            "POST",
            f"/api/v1/agents/{state['agent_id']}/versions",
            json_body={
                "system_instruction": (
                    "Plan carefully. For first-item pet detail, list pets then "
                    "fetch the first pet by id."
                ),
                "llm_profile_id": llm_profile_id,
                "selection_settings": {
                    "auto_select_threshold": 0.82,
                    "confirmation_threshold": 0.60,
                    "max_candidates": 12,
                },
                "change_summary": "pets sequential e2e v1",
            },
            expect={201},
        )
        state["agent_version_id"] = str(version["id"])
        _save_state(state_path, state)

    version = client.request(
        "GET",
        f"/api/v1/agents/{state['agent_id']}/versions/{state['agent_version_id']}",
        expect={200},
    )
    if version.get("status") == "DRAFT":
        client.request(
            "PUT",
            f"/api/v1/agents/{state['agent_id']}/versions/"
            f"{state['agent_version_id']}/tool-grants",
            json_body={
                "items": [
                    {
                        "mcp_tool_id": state["list_tool_id"],
                        "effect": "ALLOW",
                        "requires_confirmation": False,
                    },
                    {
                        "mcp_tool_id": state["detail_tool_id"],
                        "effect": "ALLOW",
                        "requires_confirmation": False,
                    },
                ]
            },
            expect={200},
        )
        client.request(
            "POST",
            f"/api/v1/agents/{state['agent_id']}/versions/"
            f"{state['agent_version_id']}/validate",
            expect={200},
        )
        version = client.request(
            "POST",
            f"/api/v1/agents/{state['agent_id']}/versions/"
            f"{state['agent_version_id']}/publish",
            expect={200},
        )
    state["agent_version_status"] = version["status"]
    _save_state(state_path, state)

    agent = client.request(
        "GET", f"/api/v1/agents/{state['agent_id']}", expect={200}
    )
    if agent.get("status") != "ACTIVE":
        agent = client.request(
            "PATCH",
            f"/api/v1/agents/{state['agent_id']}",
            json_body={
                "status": "ACTIVE",
                "lock_version": int(agent["lock_version"]),
            },
            expect={200},
        )
    state["agent_status"] = agent["status"]
    state["agent_lock_version"] = int(agent["lock_version"])
    state["phase"] = "setup_complete"
    _save_state(state_path, state)
    _log(f"agent ACTIVE id={state['agent_id']} version={state['agent_version_id']}")

    if not args.skip_embedding_wait:
        _log("waiting for ToolEmbedding READY via select probe…")
        probe = _wait_embeddings_via_select(
            client,
            agent_id=state["agent_id"],
            list_version_id=state["list_tool_version_id"],
            detail_version_id=state["detail_tool_version_id"],
            timeout_s=float(args.embedding_timeout),
        )
        state["embedding_probe"] = probe
        if probe.get("probe") == "detail_selected_directly":
            state["phase"] = "setup_warning_detail_primary"
            _save_state(state_path, state)
            _log(
                "WARNING: ToolSelector AUTO_SELECT chose detail Tool directly. "
                "Sequential pair needs list primary + companion candidate. "
                "Inspect selection.reason_summary / embeddings / summaries."
            )
            return 2
        state["phase"] = "setup_ready"
        _save_state(state_path, state)
        _log("setup ready (list primary selectable)")
    else:
        _log("skip embedding wait (--skip-embedding-wait)")
    _log(f"state written → {state_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    username = os.environ.get("MCPFLOW_USERNAME") or args.username
    password = os.environ.get("MCPFLOW_PASSWORD") or args.password
    if not username or not password:
        raise SystemExit("MCPFLOW_USERNAME / MCPFLOW_PASSWORD required for verify")
    base = os.environ.get("MCPFLOW_BASE_URL") or args.base_url
    state_path = Path(os.environ.get("MCPFLOW_STATE_PATH") or args.state_path)
    state = _load_state(state_path)
    for key in (
        "agent_id",
        "list_tool_version_id",
        "detail_tool_version_id",
    ):
        if key not in state:
            raise SystemExit(f"State missing {key}; run setup first")

    client = Client(base)
    client.login(username, password)

    created = client.request(
        "POST",
        "/api/v1/agent-requests",
        json_body={"agent_id": state["agent_id"], "request_text": NL_REQUEST},
        expect={201},
    )
    request_id = created["id"]
    state["last_request_id"] = request_id
    _save_state(state_path, state)
    _log(f"agent_request_id={request_id}")

    analyzed = client.request(
        "POST",
        f"/api/v1/agent-requests/{request_id}/analyze",
        expect={200},
    )
    structured = analyzed.get("structured_request") or {}
    _log(
        f"analyze status={analyzed.get('status')} "
        f"intent={structured.get('intent')!r} "
        f"expected_outputs={structured.get('expected_outputs')!r}"
    )
    if analyzed.get("status") != "RETRIEVING":
        raise ApiError(f"Expected RETRIEVING after analyze, got {analyzed.get('status')}")

    selection = client.request(
        "POST",
        f"/api/v1/agent-requests/{request_id}/select-tool",
        expect={200},
    )
    sel = selection.get("selection") or {}
    decision = sel.get("decision")
    selected = str(sel.get("selected_tool_version_id") or "")
    _log(
        f"select decision={decision} tool={sel.get('selected_tool_name')!r} "
        f"version={selected} reason={sel.get('reason_summary')!r}"
    )
    if decision != "AUTO_SELECT":
        raise ApiError(
            f"ToolSelector decision={decision} (need AUTO_SELECT on list). "
            f"Candidates may lack READY embeddings or grants. selection={sel}"
        )
    if selected == state["detail_tool_version_id"]:
        raise ApiError(
            "ToolSelector chose GET /pets/{pet_id} as primary. "
            "Sequential pair requires list primary + companion candidate. "
            f"reason={sel.get('reason_summary')!r}"
        )
    if selected != state["list_tool_version_id"]:
        raise ApiError(
            f"Unexpected selected_tool_version_id={selected}; "
            f"expected list={state['list_tool_version_id']}"
        )

    # Drive planning pipeline to READY.
    for action, need_status in (
        ("build-parameters", "BUILDING_PARAMETERS"),
        ("generate-plan", "PLANNING"),
        ("validate-plan", "VALIDATING"),
    ):
        progress = client.request(
            "GET", f"/api/v1/agent-requests/{request_id}/progress", expect={200}
        )
        status = (progress.get("request") or {}).get("status")
        if status != need_status:
            # Already advanced (e.g. WAITING_INPUT) — report.
            if status in {"READY", "WAITING_CONFIRMATION", "WAITING_INPUT", "REJECTED", "FAILED"}:
                break
            raise ApiError(f"Before {action}: expected {need_status}, got {status}")
        progress = client.request(
            "POST",
            f"/api/v1/agent-requests/{request_id}/{action}",
            expect={200},
        )

    progress = client.request(
        "GET", f"/api/v1/agent-requests/{request_id}/progress", expect={200}
    )
    req_status = (progress.get("request") or {}).get("status")
    plan = progress.get("plan") or {}
    _log(
        f"progress status={req_status} step_count={plan.get('step_count')} "
        f"safe_auto_execute={progress.get('safe_auto_execute')} "
        f"steps={plan.get('step_names')}"
    )
    if req_status != "READY":
        raise ApiError(f"Plan not READY: {req_status} progress={progress}")
    if int(plan.get("step_count") or 0) != 2:
        raise ApiError(
            f"Expected 2-step sequential plan, got step_count={plan.get('step_count')}. "
            "Check StructuredRequest detail intent + unique companion."
        )
    if not progress.get("safe_auto_execute"):
        raise ApiError("safe_auto_execute is false; refuse auto execution")

    exec_create = client.request(
        "POST",
        f"/api/v1/agent-requests/{request_id}/executions",
        headers={"Idempotency-Key": _idem(state["run_id"], f"exec-{request_id}")},
        expect={200, 201},
    )
    execution_id = str(exec_create.get("id") or exec_create.get("execution_id") or "")
    state["last_execution_id"] = execution_id
    _save_state(state_path, state)
    _log(f"execution_id={execution_id}")

    deadline = time.time() + float(args.exec_timeout)
    chat: dict[str, Any] | None = None
    while time.time() < deadline:
        chat = client.request(
            "GET",
            f"/api/v1/agent-requests/{request_id}/chat-result",
            expect={200},
        )
        status = chat.get("execution_status")
        if status in {
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        }:
            break
        time.sleep(2.0)
    assert chat is not None
    _log(
        f"chat-result status={chat.get('execution_status')} "
        f"available={chat.get('available')} items={chat.get('display_items')} "
        f"steps={chat.get('steps')}"
    )
    steps = chat.get("steps") or []
    if chat.get("execution_status") != "SUCCEEDED":
        raise ApiError(f"Execution not SUCCEEDED: {chat}")
    if len(steps) != 2 or any(s.get("status") != "SUCCEEDED" for s in steps):
        raise ApiError(f"Expected 2 SUCCEEDED steps, got {steps}")
    names = " ".join(
        str(i.get("name") or "") for i in (chat.get("display_items") or [])
    )
    summary = str(chat.get("summary_text") or "")
    if "Alpha" not in names and "Alpha" not in summary:
        raise ApiError(
            f"Alpha not found in chat result display/summary: {chat}"
        )
    state["phase"] = "verify_passed"
    _save_state(state_path, state)
    _log(
        "VERIFY PASS (confirmed via chat-result): "
        "execution SUCCEEDED, 2 steps SUCCEEDED, Alpha present in display/summary."
    )
    _log(
        "VERIFY NOT CHECKED by this script: tool_2 STEP_OUTPUT resolved_input "
        "(e.g. pet_id=1). chat-result does not expose bindings — inspect ops "
        "Execution/Step detail separately if you need that proof."
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(os.environ.get("MCPFLOW_STATE_PATH") or args.state_path)
    state = _load_state(state_path)
    if not state:
        _log(f"no state at {state_path}")
        return 1
    _log(json.dumps(state, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--run-id", default="pets-seq-e2e-v1")
    p.add_argument("--state-path", default=str(DEFAULT_STATE))
    p.add_argument("--openapi", default=None)
    p.add_argument("--mock-base-url", default=DEFAULT_MOCK)
    p.add_argument("--llm-profile-id", default=None)
    p.add_argument("--embedding-timeout", type=float, default=300.0)
    p.add_argument("--exec-timeout", type=float, default=180.0)
    p.add_argument(
        "--skip-embedding-wait",
        action="store_true",
        help="setup only through Agent ACTIVE (skip select probe)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("dry-run", help="Print planned steps")
    sub.add_parser("setup", help="Idempotent Factory+Agent setup")
    sub.add_parser("verify", help="Run NL sequential pair verification")
    sub.add_parser("status", help="Show local state file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "dry-run":
            return cmd_dry_run(args)
        if args.command == "setup":
            return cmd_setup(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "status":
            return cmd_status(args)
        raise SystemExit(f"unknown command {args.command}")
    except ApiError as exc:
        _log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
