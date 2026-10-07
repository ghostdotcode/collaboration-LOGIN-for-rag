"""
Office location — owns the timezone and the local working week.

A user in the NY office and one in the Dubai office have different weekends
(Sat/Sun vs Fri/Sat), so the working week cannot be a global constant.
"""

from __future__ import annotations

from typing import List

from sqlalchemy import JSON, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Location(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str | None] = mapped_column(String(80))
    # IANA name, e.g. "Asia/Tokyo". Used for every working-day calculation.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # Weekday ints per date.weekday(): 0 = Monday.
    working_days: Mapped[List[int]] = mapped_column(
        JSON, nullable=False, default=lambda: [0, 1, 2, 3, 4]
    )
    standard_hours_per_day: Mapped[int] = mapped_column(default=8, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
