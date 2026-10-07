"""
Delegation — "assign a temporary approver while I'm on leave".

Resolved at decision time rather than baked into the approval rows, so a
delegation created *after* a request was raised still takes effect.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    qualified,
)


class Delegation(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="delegation_window_valid"),
        CheckConstraint("delegator_id <> delegate_id", name="no_self_delegation"),
        Index(
            "ix_delegations_window", "tenant_id", "delegator_id", "starts_on", "ends_on"
        ),
    )

    delegator_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delegate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def covers(self, on_date: date) -> bool:
        return self.is_active and self.starts_on <= on_date <= self.ends_on
