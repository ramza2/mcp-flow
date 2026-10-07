"""ORM metadata contract for AuditEvent vs migration 20261006_0024."""

from __future__ import annotations

from app.models.audit import AuditEvent
from sqlalchemy import CHAR, BigInteger, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID


def test_audit_event_has_no_mutable_fields() -> None:
    cols = set(AuditEvent.__table__.c.keys())
    assert "updated_at" not in cols
    assert "lock_version" not in cols
    assert "deleted_at" not in cols
    assert "created_at" not in cols


def test_audit_event_index_names_and_columns_match_migration() -> None:
    expected = {
        "ix_audit_events_occurred_id": ("occurred_at", "id"),
        "ix_audit_events_actor": ("actor_type", "actor_id", "occurred_at"),
        "ix_audit_events_action": ("action", "occurred_at"),
        "ix_audit_events_resource": ("resource_type", "resource_id", "occurred_at"),
        "ix_audit_events_result": ("result", "occurred_at"),
        "ix_audit_events_request_id": ("request_id",),
        "ix_audit_events_trace_id": ("trace_id",),
        "ix_audit_events_execution_id": ("execution_id", "occurred_at"),
    }
    by_name = {idx.name: idx for idx in AuditEvent.__table__.indexes}
    assert set(by_name) == set(expected)
    # No leftover mismatched names from earlier drafts.
    assert "ix_audit_events_occurred_at" not in by_name
    assert "ix_audit_events_actor_type_actor_id" not in by_name
    for name, cols in expected.items():
        idx = by_name[name]
        assert tuple(c.name for c in idx.columns) == cols


def test_audit_event_column_contract() -> None:
    table = AuditEvent.__table__
    id_col = table.c.id
    # PostgreSQL dialect uses BigInteger; SQLite variant uses Integer.
    assert isinstance(id_col.type, (Integer, BigInteger)) or isinstance(
        getattr(id_col.type, "impl", None), (Integer, BigInteger)
    )

    assert isinstance(table.c.event_id.type, UUID) or table.c.event_id.type.python_type is object
    assert table.c.event_id.unique or any(
        c.name == "event_id" and c.unique for c in table.constraints if hasattr(c, "columns")
    ) or any(
        getattr(c, "name", None) == "uq_audit_events_event_id"
        or list(getattr(c, "columns", {}).keys()) == ["event_id"]
        for c in table.constraints
    )

    assert table.c.occurred_at.type.timezone is True
    assert table.c.actor_type.type.length == 16
    assert table.c.actor_id.type.length == 128
    assert table.c.action.type.length == 128
    assert table.c.resource_type.type.length == 64
    assert table.c.resource_id.type.length == 128
    assert table.c.result.type.length == 16
    assert table.c.request_id.type.length == 128
    assert table.c.trace_id.type.length == 128
    assert isinstance(table.c.source_ip_hash.type, CHAR)
    assert table.c.source_ip_hash.type.length == 64
    assert table.c.reason.type.length == 1000
    assert isinstance(table.c.integrity_hash.type, CHAR)
    assert table.c.integrity_hash.type.length == 64

    # JSONB snapshots
    for name in ("before_data", "after_data", "change_set"):
        assert isinstance(table.c[name].type, JSONB)

    # FK RESTRICT on execution_id
    fks = list(table.c.execution_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "RESTRICT"
    assert fks[0].column.table.name == "executions"
