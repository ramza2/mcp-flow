import logging

from app.core.logging import RequestIdFilter
from app.core.middleware import _request_id_ctx


def test_request_id_filter_reads_context() -> None:
    filter_ = RequestIdFilter()
    token = _request_id_ctx.set("canonical-test-001")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        assert filter_.filter(record) is True
        assert record.request_id == "canonical-test-001"
    finally:
        _request_id_ctx.reset(token)


def test_request_id_filter_defaults_without_context() -> None:
    token = _request_id_ctx.set(None)
    try:
        filter_ = RequestIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="startup",
            args=(),
            exc_info=None,
        )
        assert filter_.filter(record) is True
        assert record.request_id == "-"
    finally:
        _request_id_ctx.reset(token)


def test_request_id_filter_preserves_explicit_adapter_value() -> None:
    token = _request_id_ctx.set("context-id")
    try:
        filter_ = RequestIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="bound",
            args=(),
            exc_info=None,
        )
        record.request_id = "explicit-adapter-id"
        assert filter_.filter(record) is True
        assert record.request_id == "explicit-adapter-id"
    finally:
        _request_id_ctx.reset(token)
