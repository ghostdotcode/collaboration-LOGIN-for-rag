"""
LeaveBalance — one row per (user, leave type, leave year).

This is the row the double-booking race hinges on. Every deduction path
locks it with `SELECT ... FOR UPDATE` (see `services/leave_request_service`),
and `row_version` gives us an optimistic-locking backstop for any code path
that legitimately reads before it writes.

`pending_days` is an encumbrance: days reserved by submitted-but-not-yet-
approved requests. Without it, two pending requests could each pass the
balance check and only collide at approval time.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    qualified,
)

ZERO = Decimal("0.00")


class LeaveBalance(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "leave_type_id", "year"),
        CheckConstraint("used_days >= 0", name="used_days_non_negative"),
        CheckConstraint("pending_days >= 0", name="pending_days_non_negative"),
        CheckConstraint("accrued_days >= 0", name="accrued_days_non_negative"),
        CheckConstraint("year BETWEEN 1970 AND 2200", name="year_sane"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_types") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Leave year, keyed by its starting calendar year (tenants may run Apr-Mar).
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # The policy version these numbers were computed under. Retroactive
    # policy edits create a new version and leave this pointer untouched.
    policy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("leave_policies") + ".id", ondelete="SET NULL"),
    )

    opening_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    accrued_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    rolled_over_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    used_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    pending_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    encashed_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )
    expired_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=ZERO
    )

    # Idempotency guard for the nightly accrual job: a re-run for the same
    # date is a no-op instead of double-crediting.
    last_accrued_on: Mapped[Optional[date]] = mapped_column(Date)
    rollover_expires_on: Mapped[Optional[date]] = mapped_column(Date)

    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    leave_type: Mapped["LeaveType"] = relationship(lazy="selectin")

    __mapper_args__ = {"version_id_col": row_version}

    @hybrid_property
    def entitled_days(self) -> Decimal:
        """Everything credited to the user this year."""
        return self.opening_days + self.accrued_days + self.rolled_over_days

    @hybrid_property
    def available_days(self) -> Decimal:
        """
        What the user may still book right now.

        Pending days are subtracted so an in-flight request cannot be
        spent twice.
        """
        return (
            self.opening_days
            + self.accrued_days
            + self.rolled_over_days
            - self.used_days
            - self.pending_days
            - self.encashed_days
            - self.expired_days
        )


from app.models.leave_type import LeaveType  # noqa: E402
