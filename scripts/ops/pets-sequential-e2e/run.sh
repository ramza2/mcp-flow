#!/usr/bin/env bash
# Thin wrapper for pets sequential 2-Tool E2E ops helper.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/pets_sequential_e2e.py" "$@"
