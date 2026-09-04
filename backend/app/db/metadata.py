"""SQLAlchemy metadata export for Alembic."""

from app.db.base import Base, metadata

# Ensure model tables are registered on Base.metadata.
from app import models as _models  # noqa: F401

__all__ = ["Base", "metadata"]
