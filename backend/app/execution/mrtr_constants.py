"""MRTR runtime constants (docs/04 §15 / FNC-EXE-008)."""

from __future__ import annotations

# Bounded rounds per logical Tool Attempt (OPEN waits). Unbounded MRTR is forbidden.
MAX_MRTR_ROUNDS = 8
