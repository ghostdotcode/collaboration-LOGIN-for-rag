"""
Resolves an employee's `WorkCalendar` — their local working week plus the
holidays that apply at *their* office.

This is the layer that keeps the New York employee from having Diwali
deducted and the Bengaluru employee from having Thanksgiving deducted.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.holiday_calendar import Holiday, HolidayCalendar
from app.models.location import Location
from app.models.user import User
from app.services.leave_calculator import WorkCalendar


async def holidays_for(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    location_id: Optional[uuid.UUID],
    years: Sequence[int],
    include_optional: bool = False,
) -> List[Holiday]:
    """
    Holidays applying to a location for the given years.

    Matches the location's own calendars plus any tenant-wide calendar
    (`location_id IS NULL`), so a tenant can keep one global list and only
    override where offices differ.
    """
    if not years:
        return []

    location_filter = HolidayCalendar.location_id.is_(None)
    if location_id is not None:
        location_filter = or_(
            HolidayCalendar.location_id == location_id,
            HolidayCalendar.location_id.is_(None),
        )

    stmt = (
        select(Holiday)
        .join(HolidayCalendar, Holiday.calendar_id == HolidayCalendar.id)
        .where(
            and_(
                Holiday.tenant_id == tenant_id,
                HolidayCalendar.is_active.is_(True),
                HolidayCalendar.year.in_(list(years)),
                location_filter,
            )
        )
        .order_by(Holiday.holiday_date)
    )
    if not include_optional:
        # Optional/restricted holidays still cost the employee a day, so they
        # must not be treated as automatically non-working.
        stmt = stmt.where(Holiday.is_optional.is_(False))

    result = await session.scalars(stmt)
    return list(result)


async def work_calendar_for_user(
    session: AsyncSession,
    user: User,
    *,
    years: Sequence[int],
) -> WorkCalendar:
    """Build the calendar used for every duration calculation for this user."""
    location: Optional[Location] = None
    if user.location_id is not None:
        location = await session.get(Location, user.location_id)

    working_days = (
        location.working_days
        if location and location.working_days
        else settings.DEFAULT_WORKING_DAYS
    )
    hours_per_day = location.standard_hours_per_day if location else 8

    holidays = await holidays_for(
        session,
        tenant_id=user.tenant_id,
        location_id=user.location_id,
        years=years,
    )
    return WorkCalendar.build(
        working_days=working_days,
        holidays=[h.holiday_date for h in holidays],
        standard_hours_per_day=Decimal(str(hours_per_day)),
    )


async def upcoming_holidays(
    session: AsyncSession,
    user: User,
    *,
    from_date: date,
    limit: int = 5,
) -> List[Holiday]:
    """Dashboard widget: the next few public holidays at the user's office."""
    location_filter = HolidayCalendar.location_id.is_(None)
    if user.location_id is not None:
        location_filter = or_(
            HolidayCalendar.location_id == user.location_id,
            HolidayCalendar.location_id.is_(None),
        )

    stmt = (
        select(Holiday)
        .join(HolidayCalendar, Holiday.calendar_id == HolidayCalendar.id)
        .where(
            and_(
                Holiday.tenant_id == user.tenant_id,
                Holiday.holiday_date >= from_date,
                HolidayCalendar.is_active.is_(True),
                location_filter,
            )
        )
        .order_by(Holiday.holiday_date)
        .limit(limit)
    )
    result = await session.scalars(stmt)
    return list(result)
