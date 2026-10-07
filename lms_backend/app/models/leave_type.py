"""
LeaveType — the *kind* of leave (Annual, Sick, Maternity, Comp-Off...).

Rules that are intrinsic to the type live here; anything with a temporal
dimension (quotas, accrual rates) lives on LeavePolicy so it can be
versioned without rewriting history.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import LeaveUnit
from app.models.sa_types import sa_enum


class LeaveType(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        CheckConstraint(
            "max_consecutive_days IS NULL OR max_consecutive_days > 0",
            name="max_consecutive_days_positive",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    unit: Mapped[LeaveUnit] = mapped_column(
        sa_enum(LeaveUnit, "leave_unit"), nullable=False, default=LeaveUnit.DAY
    )

    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    allow_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_hourly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "Requires a medical certificate if > 2 days" is expressed here.
    attachment_required_above_days: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 2)
    )
    max_consecutive_days: Mapped[Optional[int]] = mapped_column(Integer)
    min_notice_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Retroactive (back-dated) applications allowed at all? Sick leave: yes.
    allow_retroactive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Codes of leave types this one may not be clubbed with, e.g.
    # sick leave adjacent to annual leave. Enforced in leave_request_service.
    cannot_club_with: Mapped[List[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Free-form eligibility predicate, e.g. {"gender": "female",
    # "min_tenure_months": 12} for maternity leave.
    eligibility_rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    color_hex: Mapped[str] = mapped_column(String(7), nullable=False, default="#6366f1")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
