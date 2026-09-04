#!/usr/bin/env python3
"""Cross-platform local smoke checks for MCPFlow Compose (stdlib only)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def _request(url: str, *, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, exc.read()


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"OK: {message}")


def check_frontend_root(base: str) -> None:
    status, _headers, body = _request(f"{base}/")
    if status != 200:
        _fail(f"GET / expected 200, got {status}")
    text = body.decode("utf-8", errors="replace").lower()
    if "<html" not in text and "<!doctype html" not in text:
        _fail("GET / did not return HTML")
    if "mcpflow" not in text and "root" not in text and "vite" not in text and "id=\"root\"" not in text:
        # Production build still embeds app shell; accept generic SPA HTML with #root.
        if 'id="root"' not in text and "id='root'" not in text:
            _fail("GET / HTML did not look like MCPFlow frontend SPA")
    _ok("GET / → 200 HTML")


def check_live(base: str) -> None:
    status, headers, body = _request(f"{base}/health/live")
    if status != 200:
        _fail(f"/health/live expected 200, got {status}")
    payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    if payload.get("status") != "ok":
        _fail(f"/health/live unexpected body: {payload}")
    if not headers.get("x-request-id"):
        _fail("/health/live missing X-Request-ID")
    _ok("GET /health/live → 200 status=ok + X-Request-ID")


def check_ready(base: str) -> None:
    status, _headers, body = _request(f"{base}/health/ready")
    if status != 200:
        _fail(f"/health/ready expected 200, got {status} body={body!r}")
    payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    if payload.get("status") != "ok":
        _fail(f"/health/ready status not ok: {payload}")
    checks = payload.get("checks") or {}
    if checks.get("database") != "ok":
        _fail(f"/health/ready database not ok: {payload}")
    _ok("GET /health/ready → 200 status=ok database=ok")


def check_api_not_frontend(base: str) -> None:
    status, headers, body = _request(f"{base}/api/v1/__infra-smoke-missing")
    if status != 404:
        _fail(f"/api/v1/__infra-smoke-missing expected 404, got {status}")
    content_type = headers.get("content-type", "")
    if "application/json" not in content_type:
        _fail(f"expected application/json, got {content_type!r}")
    payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    error = payload.get("error") or {}
    if error.get("code") != "NOT_FOUND":
        _fail(f"expected error.code=NOT_FOUND, got {payload}")
    if not headers.get("x-request-id"):
        _fail("API 404 missing X-Request-ID")
    _ok("GET /api/v1/__infra-smoke-missing → Backend JSON 404")


def check_spa_routes(base: str, paths: list[str]) -> None:
    for path in paths:
        status, headers, body = _request(f"{base}{path}")
        if status != 200:
            _fail(f"SPA {path} expected 200, got {status}")
        content_type = headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            # Some servers omit charset nuances; still require HTML body.
            text = body.decode("utf-8", errors="replace").lower()
            if "<html" not in text and "<!doctype html" not in text:
                _fail(f"SPA {path} did not return HTML (content-type={content_type!r})")
        _ok(f"GET {path} → 200 HTML fallback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPFlow local Compose smoke checks")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Traefik entrypoint base URL",
    )
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")

    print(f"smoke base={base}")
    check_frontend_root(base)
    check_live(base)
    check_ready(base)
    check_api_not_frontend(base)
    check_spa_routes(base, ["/agents", "/workflows", "/executions", "/mcp/servers"])
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
