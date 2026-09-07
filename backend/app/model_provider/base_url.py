"""OpenAI-compatible base_url normalization."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_openai_compatible_root(base_url: str) -> str:
    """Return API root for OpenAI-compatible endpoints (…/v1).

    Rules:
    - strip trailing slashes
    - if path already ends with ``/v1``, keep it
    - otherwise append ``/v1``
    - never produce ``/v1/v1`` or double slashes
    """

    raw = (base_url or "").strip()
    parsed = urlparse(raw)
    path = parsed.path or ""
    # Collapse duplicate slashes in path and strip trailing slash.
    parts = [segment for segment in path.split("/") if segment]
    if parts and parts[-1] == "v1":
        normalized_path = "/" + "/".join(parts)
    else:
        parts.append("v1")
        normalized_path = "/" + "/".join(parts)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            normalized_path,
            "",
            "",
            "",
        )
    )


def join_api_path(api_root: str, relative: str) -> str:
    """Join ``api_root`` with a relative path like ``models`` or ``embeddings``."""

    root = api_root.rstrip("/")
    rel = relative.lstrip("/")
    return f"{root}/{rel}"
