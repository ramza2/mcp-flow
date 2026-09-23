"""Dialect-isolated SQL predicates for ApprovalRequest approver visibility.

PostgreSQL uses JSONB operators. SQLite (API unit tests) uses json_extract /
json_each. Do not weaken PostgreSQL semantics for SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, false, or_, text

from app.models.approval import ApprovalRequest


def scope_visibility_clause(
    *,
    actor_role_codes: Sequence[str],
    dialect_name: str,
) -> ColumnElement[bool]:
    """True when ApprovalRequest.approval_scope allows the actor.

    Supported shapes only (same as validate_approver_scope):
    null, {}, {"role_codes": ["ROLE_A", ...]}.
    Malformed scopes do not match (fail closed for list visibility).
    """
    if dialect_name == "postgresql":
        return _pg_scope_clause(actor_role_codes=actor_role_codes)
    return _sqlite_scope_clause(actor_role_codes=actor_role_codes)


def self_approval_visibility_clause(
    *,
    actor_user_id: uuid.UUID,
    dialect_name: str,
) -> ColumnElement[bool]:
    """True when snapshotted allow_self_approval permits the actor.

    Non-requesters always pass. Requesters require
    context_snapshot.approval_policy.allow_self_approval == true.
    Missing/corrupt flag → requester excluded (fail closed).
    """
    not_requester = ApprovalRequest.requested_by != actor_user_id
    if dialect_name == "postgresql":
        allow_self = (
            ApprovalRequest.context_snapshot["approval_policy"]["allow_self_approval"]
            .as_boolean()
            .is_(True)
        )
        return or_(not_requester, allow_self)

    allow_self = text(
        "lower(coalesce(json_extract(approval_requests.context_snapshot, "
        "'$.approval_policy.allow_self_approval'), 'false')) IN ('true', '1')"
    )
    return or_(not_requester, allow_self)


def _open_scope_clause(*, dialect_name: str) -> ColumnElement[bool]:
    scope = ApprovalRequest.approval_scope
    if dialect_name == "postgresql":
        return or_(
            scope.is_(None),
            text("approval_requests.approval_scope = '{}'::jsonb"),
            # JSON null is not used by SQLAlchemy for None, but accept it fail-open-safe.
            text("approval_requests.approval_scope = 'null'::jsonb"),
        )
    # SQLite JSON columns often persist Python None as the text 'null'.
    return or_(
        scope.is_(None),
        text(
            "("
            "  approval_requests.approval_scope = 'null'"
            "  OR json_type(approval_requests.approval_scope) = 'null'"
            "  OR approval_requests.approval_scope = '{}'"
            "  OR ("
            "    json_type(approval_requests.approval_scope) = 'object'"
            "    AND ("
            "      SELECT count(*) FROM json_each(approval_requests.approval_scope)"
            "    ) = 0"
            "  )"
            ")"
        ),
    )


def _pg_scope_clause(*, actor_role_codes: Sequence[str]) -> ColumnElement[bool]:
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY, TEXT

    open_scope = _open_scope_clause(dialect_name="postgresql")
    if not actor_role_codes:
        return open_scope

    role_match = text(
        "("
        "  jsonb_typeof(approval_requests.approval_scope) = 'object'"
        "  AND approval_requests.approval_scope ? 'role_codes'"
        "  AND ("
        "    SELECT count(*) FROM jsonb_object_keys(approval_requests.approval_scope)"
        "  ) = 1"
        "  AND jsonb_typeof(approval_requests.approval_scope->'role_codes') = 'array'"
        "  AND jsonb_array_length(approval_requests.approval_scope->'role_codes') > 0"
        "  AND EXISTS ("
        "    SELECT 1 FROM jsonb_array_elements_text("
        "      approval_requests.approval_scope->'role_codes'"
        "    ) AS req(code)"
        "    WHERE req.code = ANY(:actor_role_codes)"
        "  )"
        ")"
    ).bindparams(
        bindparam(
            "actor_role_codes",
            value=list(actor_role_codes),
            type_=ARRAY(TEXT),
        )
    )
    return or_(open_scope, role_match)


def _sqlite_scope_clause(*, actor_role_codes: Sequence[str]) -> ColumnElement[bool]:
    open_scope = _open_scope_clause(dialect_name="sqlite")
    if not actor_role_codes:
        return open_scope

    bound = [
        text(
            "EXISTS ("
            "  SELECT 1 FROM json_each("
            "    json_extract(approval_requests.approval_scope, '$.role_codes')"
            "  ) AS je WHERE je.value = :code"
            ")"
        ).bindparams(code=code)
        for code in actor_role_codes
    ]
    role_array_match: ColumnElement[bool] = or_(*bound) if bound else false()

    exactly_role_codes_key = text(
        "("
        "  json_type(approval_requests.approval_scope) = 'object'"
        "  AND ("
        "    SELECT count(*) FROM json_each(approval_requests.approval_scope)"
        "  ) = 1"
        "  AND json_extract(approval_requests.approval_scope, '$.role_codes') IS NOT NULL"
        "  AND json_type("
        "    json_extract(approval_requests.approval_scope, '$.role_codes')"
        "  ) = 'array'"
        "  AND json_array_length("
        "    json_extract(approval_requests.approval_scope, '$.role_codes')"
        "  ) > 0"
        ")"
    )
    return or_(open_scope, and_(exactly_role_codes_key, role_array_match))
