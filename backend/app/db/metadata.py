"""SQLAlchemy metadata export for Alembic."""

# Ensure model tables are registered on Base.metadata.
from app import models as _models  # noqa: F401
from app.db.base import Base, metadata

__all__ = ["Base", "metadata"]
