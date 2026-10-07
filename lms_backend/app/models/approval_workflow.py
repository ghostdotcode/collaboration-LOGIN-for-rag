"""
Custom approval chains (Employee -> Manager -> HR) and their per-request
instances.

`ApprovalWorkflow` + `ApprovalWorkflowStep` are the *template* HR drags
together in the UI. `LeaveApproval` is the materialised instance: one row
per level per request, which is what makes "who approved what, when, from
which IP" answerable years later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    qualified,
)
from app.models.enums import ActionChannel, ApprovalStatus, ApproverType, UserRole
from app.models.sa_types import sa_enum


class ApprovalWorkflow(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "approval_workflows"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Null selectors broaden applicability; the most specific match wins.
    leave_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_types") + ".id", ondelete="CASCADE"),
        index=True,
    )
    applies_to_location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("locations") + ".id", ondelete="CASCADE"),
    )
    applies_to_role: Mapped[Optional[UserRole]] = mapped_column(
        sa_enum(UserRole, "user_role")
    )
    # Long absences can demand a deeper chain than a one-day request.
    min_duration_days: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    steps: Mapped[List["ApprovalWorkflowStep"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="ApprovalWorkflowStep.level",
        lazy="selectin",
    )


class ApprovalWorkflowStep(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "approval_workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_id", "level"),
        CheckConstraint("level > 0", name="level_positive"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("approval_workflows") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_type: Mapped[ApproverType] = mapped_column(
        sa_enum(ApproverType, "approver_type"),
        nullable=False,
        default=ApproverType.REPORTING_MANAGER,
    )
    # Only for SPECIFIC_USER / ROLE resolution respectively.
    approver_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(qualified("users") + ".id", ondelete="SET NULL")
    )
    approver_role: Mapped[Optional[UserRole]] = mapped_column(
        sa_enum(UserRole, "user_role")
    )
    # Escalate (or auto-approve) if the approver sits on it too long.
    auto_approve_after_hours: Mapped[Optional[int]] = mapped_column(Integer)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workflow: Mapped["ApprovalWorkflow"] = relationship(back_populates="steps")


class LeaveApproval(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_approvals"
    __table_args__ = (
        UniqueConstraint("leave_request_id", "level"),
        CheckConstraint("level > 0", name="level_positive"),
    )

    leave_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_requests") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="SET NULL"),
        index=True,
    )
    # Set when a delegate acted while the real approver was away.
    delegated_from_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(qualified("users") + ".id", ondelete="SET NULL")
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        sa_enum(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        index=True,
    )
    comment: Mapped[Optional[str]] = mapped_column(Text)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    channel: Mapped[ActionChannel] = mapped_column(
        sa_enum(ActionChannel, "action_channel"),
        nullable=False,
        default=ActionChannel.WEB,
    )
    # Recorded for the audit trail of email one-click approvals.
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(256))

    leave_request: Mapped["LeaveRequest"] = relationship(back_populates="approvals")
    approver: Mapped[Optional["User"]] = relationship(
        foreign_keys=[approver_id], lazy="selectin"
    )


from app.models.leave_request import LeaveRequest  # noqa: E402
from app.models.user import User  # noqa: E402
