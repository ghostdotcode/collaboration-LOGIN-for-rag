"""
LeavePolicy — an *immutable, versioned* snapshot of leave entitlement rules.

Rows are append-only. When HR drops annual leave from 20 to 15 days in
October we insert version N+1 with `effective_from = 2026-10-01` and close
version N by setting its `effective_to`; historical balances keep pointing
at the version that was live when they were computed, so past accruals
never silently change.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
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
from app.models.enums import AccrualFrequency, UserRole
from app.models.sa_types import sa_enum


class LeavePolicy(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "leave_type_id", "version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_window_valid",
        ),
        CheckConstraint("annual_quota_days >= 0", name="annual_quota_non_negative"),
        CheckConstraint("accrual_rate_days >= 0", name="accrual_rate_non_negative"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_types") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Null `effective_to` == currently in force.
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, index=True)
    previous_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_policies") + ".id", ondelete="SET NULL"),
    )

    # ── Entitlement & accrual ────────────────────────────────────────────
    annual_quota_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    accrual_frequency: Mapped[AccrualFrequency] = mapped_column(
        sa_enum(AccrualFrequency, "accrual_frequency"),
        nullable=False,
        default=AccrualFrequency.MONTHLY,
    )
    # Days credited per accrual period, e.g. 1.50 days/month.
    accrual_rate_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    # Pro-rate the first year for mid-month / mid-year joiners.
    prorate_on_joining: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # No accrual until probation clears.
    accrual_starts_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # ── Year-end behaviour ───────────────────────────────────────────────
    max_rollover_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    # Carried-over days lapse this many months into the new year (0 = never).
    rollover_expiry_months: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    allow_encashment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ── Borrowing ────────────────────────────────────────────────────────
    allow_negative_balance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    max_negative_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )

    # ── Applicability (null = applies to everyone) ───────────────────────
    applies_to_role: Mapped[Optional[UserRole]] = mapped_column(
        sa_enum(UserRole, "user_role")
    )
    applies_to_location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("locations") + ".id", ondelete="CASCADE"),
    )
    min_tenure_months: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    leave_type: Mapped["LeaveType"] = relationship(lazy="selectin")

    def is_in_force_on(self, on_date: date) -> bool:
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to

    @property
    def accrual_periods_per_year(self) -> int:
        return {
            AccrualFrequency.MONTHLY: 12,
            AccrualFrequency.QUARTERLY: 4,
            AccrualFrequency.ANNUALLY: 1,
            AccrualFrequency.NONE: 0,
        }[self.accrual_frequency]


from app.models.leave_type import LeaveType  # noqa: E402
