"""Shared pagination helpers (docs/06)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SortDirection = Literal["asc", "desc"]

ALLOWED_SERVER_SORT = {"updated_at", "created_at", "name", "status"}
ALLOWED_TOOL_SORT = {"updated_at", "created_at", "remote_name", "status"}


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort: str = "-updated_at"


class Page(BaseModel):
    items: list[Any]
    page: int
    page_size: int
    total: int
    has_next: bool


def parse_sort(sort: str, *, allowed: set[str], default_field: str = "updated_at") -> tuple[str, SortDirection]:
    raw = (sort or f"-{default_field}").strip()
    direction: SortDirection = "desc"
    field = raw
    if raw.startswith("-"):
        direction = "desc"
        field = raw[1:]
    elif raw.startswith("+"):
        direction = "asc"
        field = raw[1:]
    if field not in allowed:
        field = default_field
        direction = "desc"
    return field, direction
