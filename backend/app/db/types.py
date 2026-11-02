"""Custom SQLAlchemy column types.

Only one is needed so far: a portable UUID column. MySQL has no native UUID
type that Alembic renders consistently, and SQLite has none at all, so the
identifier is stored as ``CHAR(36)`` and converted to :class:`uuid.UUID` in
Python. Keeping the conversion in one place means no caller ever has to care
whether a value came back as ``str`` or ``UUID`` depending on the driver.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CHAR
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

UUID_CHAR_LENGTH = 36


class GUID(TypeDecorator[uuid.UUID]):
    """Store a UUID as a 36-character ASCII string, portable across dialects."""

    impl = CHAR
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(length=UUID_CHAR_LENGTH)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        # Validating rather than passing the string through keeps a typo in a
        # fixture from becoming a silently-unmatchable row.
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
