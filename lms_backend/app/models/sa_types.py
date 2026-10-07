"""
Shared SQLAlchemy ENUM instances.

Postgres enums are database-level objects, so the same logical enum used by
two tables must be backed by *one* type instance bound to the metadata —
otherwise DDL tries to `CREATE TYPE user_role` twice and blows up.
"""

from enum import Enum as PyEnum
from typing import Dict, Type

from sqlalchemy import Enum as SAEnum

from app.db.base_class import Base

_REGISTRY: Dict[str, SAEnum] = {}


def sa_enum(enum_cls: Type[PyEnum], name: str) -> SAEnum:
    """Return the process-wide ENUM type for `enum_cls`, creating it once."""
    if name not in _REGISTRY:
        _REGISTRY[name] = SAEnum(
            enum_cls,
            name=name,
            metadata=Base.metadata,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=True,
            validate_strings=True,
        )
    return _REGISTRY[name]
