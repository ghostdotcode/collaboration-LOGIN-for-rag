"""
Declarative base plus the mixins every LMS table is built from.

`TenantMixin` is the linchpin of multi-tenancy: it injects `tenant_id` into
the model *and* is what the Postgres Row-Level Security policies key off
(see the initial Alembic migration). Even if application code forgets a
`WHERE tenant_id = ...`, RLS makes cross-tenant reads impossible.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid, func, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from app.core.config import settings

# Predictable constraint/index names so Alembic autogenerate produces stable
# diffs instead of relying on Postgres' auto-naming.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Empty DB_SCHEMA => use the connection's default schema. Keeps the models
# usable on backends without schema support (e.g. SQLite in fast unit tests).
DB_SCHEMA: Optional[str] = settings.DB_SCHEMA or None


class Base(DeclarativeBase):
    metadata = MetaData(schema=DB_SCHEMA, naming_convention=NAMING_CONVENTION)

    def to_dict(self, exclude: tuple[str, ...] = ()) -> Dict[str, Any]:
        """
        Shallow column dump — used for audit-log before/after snapshots.

        Reads only what is already in the identity map. A plain `getattr` on
        an expired or server-defaulted column (`created_at` immediately after
        a flush) emits a lazy SELECT, which the async engine cannot service
        from synchronous code — so such columns are reported as None instead.
        """
        loaded = inspect(self).dict
        return {
            column.key: loaded.get(column.key)
            for column in self.__table__.columns
            if column.key not in exclude
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def qualified(table: str) -> str:
    """Schema-qualified table name for use in ForeignKey() strings."""
    return f"{DB_SCHEMA}.{table}" if DB_SCHEMA else table


class UUIDPrimaryKeyMixin:
    """Opaque UUID PKs — no tenant can enumerate another tenant's row IDs."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    # Both a Python-side and a server-side default: the server default keeps
    # rows correct for inserts made outside the ORM (migrations, psql), while
    # the Python default puts the value on the instance at flush time. Without
    # the latter, reading `created_at` on a just-created object triggers a
    # refresh — illegal mid-serialisation on the async engine.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Adds the tenant discriminator that RLS and every query filter on."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey(qualified("tenants") + ".id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
