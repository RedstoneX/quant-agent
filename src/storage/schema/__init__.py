"""Database schema package: table creation + migration ladder (lifted from src/storage/db.py)."""

from src.storage.schema.manager import DatabaseSchema

__all__ = ["DatabaseSchema"]
