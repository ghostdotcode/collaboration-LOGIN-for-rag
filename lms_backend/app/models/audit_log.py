"""
AuditLog — append-only ledger of every state change.

Deliberately *not* a `TimestampMixin` table: there is no `updated_at`
because rows are never updated. The migration additionally revokes
UPDATE/DELETE on this table from the application role.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import (
    Base,
    TenantMixin,
    UUIDPrimaryKeyMixin,
    qualified,
)
from app.models.enums import ActionChannel, AuditAction
from app.models.sa_types import sa_enum


class AuditLog(Base, UUIDPrimaryKeyMixin, TenantMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_occurred", "tenant_id", "occurred_at"),
    )

    # Nullable: system jobs (nightly accrual) have no human actor.
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="SET NULL"),
        index=True,
    )
    # Denormalised so the trail survives the user row being deleted.
    actor_email: Mapped[Optional[str]] = mapped_column(String(255))

    action: Mapped[AuditAction] = mapped_column(
        sa_enum(AuditAction, "audit_action"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True))

    before_state: Mapped[Optional[dict]] = mapped_column(JSON)
    after_state: Mapped[Optional[dict]] = mapped_column(JSON)

    channel: Mapped[ActionChannel] = mapped_column(
        sa_enum(ActionChannel, "action_channel"),
        nullable=False,
        default=ActionChannel.WEB,
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(256))
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
