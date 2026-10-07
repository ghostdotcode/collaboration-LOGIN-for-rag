"""
Geo-specific public holidays.

Calendars are scoped to a location (or left global for the tenant), because
someone in the New York office must not have Diwali deducted as a working
day — and vice versa for the Bengaluru office and Thanksgiving.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
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


class HolidayCalendar(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "holiday_calendars"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "year"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(2))
    # Null location = tenant-wide fallback calendar.
    location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("locations") + ".id", ondelete="CASCADE"),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    holidays: Mapped[List["Holiday"]] = relationship(
        back_populates="calendar",
        cascade="all, delete-orphan",
        order_by="Holiday.holiday_date",
        lazy="selectin",
    )


class Holiday(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("calendar_id", "holiday_date", "name"),)

    calendar_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("holiday_calendars") + ".id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    holiday_date: Mapped[date] = mapped_column(nullable=False, index=True)
    # Optional/restricted holidays are *not* auto-excluded from working days;
    # the employee must spend one of their allotted picks.
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    calendar: Mapped["HolidayCalendar"] = relationship(back_populates="holidays")
