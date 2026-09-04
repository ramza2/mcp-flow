from __future__ import annotations

import logging
import sys
from typing import Any

from app.core.config import Settings
from app.core.middleware import get_request_id


class RequestIdFilter(logging.Filter):
    """Inject request_id from request context when the record does not already have one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or "-"
        return True


def configure_logging(settings: Settings) -> None:
    """Configure logging with timestamp, level, logger, message, and request_id support."""

    root = logging.getLogger()
    if root.handlers:
        # Avoid duplicate handlers on reload / repeated create_app in tests.
        root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RequestIdFilter())

    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Keep noisy libraries quieter by default.
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_request_id(logger: logging.Logger, request_id: str) -> logging.LoggerAdapter[Any]:
    return logging.LoggerAdapter(logger, {"request_id": request_id})
