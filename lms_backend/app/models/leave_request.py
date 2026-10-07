"""LeaveRequest — the core transactional record."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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
from app.models.enums import DayPart, LeaveStatus
from app.models.sa_types import sa_enum


class LeaveRequest(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="date_range_valid"),
        CheckConstraint("duration_days > 0", name="duration_positive"),
        Index(
            "ix_leave_requests_tenant_user_dates",
            "tenant_id",
            "user_id",
            "start_date",
            "end_date",
        ),
        Index("ix_leave_requests_tenant_status", "tenant_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_types") + ".id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Policy version in force when the request was raised, so a later policy
    # edit can't retroactively change what this request cost.
    policy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_policies") + ".id", ondelete="SET NULL"),
    )
    # Which leave year the days were debited from.
    balance_year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Calendar dates as the *employee* experiences them locally. The UTC
    # instants below are what schedulers and cross-timezone views compare.
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_day_part: Mapped[DayPart] = mapped_column(
        sa_enum(DayPart, "day_part"), nullable=False, default=DayPart.FULL_DAY
    )
    end_day_part: Mapped[DayPart] = mapped_column(
        sa_enum(DayPart, "day_part"), nullable=False, default=DayPart.FULL_DAY
    )
    starts_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ends_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Snapshot of the requester's timezone — a later relocation must not
    # silently re-interpret a historical request.
    requester_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )

    duration_days: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    duration_hours: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))

    status: Mapped[LeaveStatus] = mapped_column(
        sa_enum(LeaveStatus, "leave_status"),
        nullable=False,
        default=LeaveStatus.PENDING,
        index=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(Text)
    attachment_url: Mapped[Optional[str]] = mapped_column(String(512))
    contact_number: Mapped[Optional[str]] = mapped_column(String(32))

    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("approval_workflows") + ".id", ondelete="SET NULL"),
    )

    is_retroactive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "3 other people in your department are away" — captured at submit time
    # so approvers see what the employee was warned about.
    conflict_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[Optional[str]] = mapped_column(Text)

    user: Mapped["User"] = relationship(lazy="selectin", foreign_keys=[user_id])
    leave_type: Mapped["LeaveType"] = relationship(lazy="selectin")
    approvals: Mapped[List["LeaveApproval"]] = relationship(
        back_populates="leave_request",
        cascade="all, delete-orphan",
        order_by="LeaveApproval.level",
        lazy="selectin",
    )

    @property
    def is_open(self) -> bool:
        return self.status == LeaveStatus.PENDING

    @property
    def is_half_day(self) -> bool:
        return self.duration_days == Decimal("0.50")


from app.models.approval_workflow import LeaveApproval  # noqa: E402
from app.models.leave_type import LeaveType  # noqa: E402
from app.models.user import User  # noqa: E402
