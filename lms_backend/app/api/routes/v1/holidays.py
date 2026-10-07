"""Holiday calendar endpoints (geo-specific)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_current_user, get_request_context, require_hr
from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.models.enums import AuditAction
from app.models.holiday_calendar import Holiday, HolidayCalendar
from app.models.user import User
from app.schemas.holiday import (
    HolidayCalendarCreate,
    HolidayCalendarRead,
    UpcomingHoliday,
)
from app.services import audit_service, calendar_service

router = APIRouter(tags=["Holidays"])


@router.get(
    "/holidays/upcoming",
    response_model=List[UpcomingHoliday],
    summary="Next public holidays at my office",
)
async def upcoming(
    limit: int = Query(default=5, ge=1, le=25),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[UpcomingHoliday]:
    """Location-aware: a New York employee never sees Bengaluru's holidays."""
    today = datetime.now(timezone.utc).date()
    holidays = await calendar_service.upcoming_holidays(
        session, user, from_date=today, limit=limit
    )
    return [
        UpcomingHoliday(
            name=holiday.name,
            holiday_date=holiday.holiday_date,
            days_away=(holiday.holiday_date - today).days,
            is_optional=holiday.is_optional,
        )
        for holiday in holidays
    ]


@router.get(
    "/holiday-calendars",
    response_model=List[HolidayCalendarRead],
    summary="List holiday calendars",
)
async def list_calendars(
    year: Optional[int] = Query(default=None, ge=1970, le=2200),
    location_id: Optional[uuid.UUID] = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[HolidayCalendarRead]:
    conditions = [HolidayCalendar.tenant_id == user.tenant_id]
    if year is not None:
        conditions.append(HolidayCalendar.year == year)
    if location_id is not None:
        conditions.append(HolidayCalendar.location_id == location_id)

    rows = await session.scalars(
        select(HolidayCalendar)
        .where(and_(*conditions))
        .order_by(HolidayCalendar.year.desc(), HolidayCalendar.name)
    )
    return [HolidayCalendarRead.model_validate(row) for row in rows]


@router.post(
    "/holiday-calendars",
    response_model=HolidayCalendarRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a holiday calendar (HR)",
)
async def create_calendar(
    payload: HolidayCalendarCreate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> HolidayCalendarRead:
    calendar = HolidayCalendar(
        tenant_id=actor.tenant_id,
        name=payload.name,
        year=payload.year,
        country_code=payload.country_code,
        location_id=payload.location_id,
    )
    session.add(calendar)
    await session.flush()

    for entry in payload.holidays:
        session.add(
            Holiday(
                tenant_id=actor.tenant_id,
                calendar_id=calendar.id,
                name=entry.name,
                holiday_date=entry.holiday_date,
                is_optional=entry.is_optional,
            )
        )
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.CREATE,
        entity_type="holiday_calendar",
        entity_id=calendar.id,
        actor_id=actor.id,
        actor_email=actor.email,
        after={
            "name": calendar.name,
            "year": calendar.year,
            "holidays": len(payload.holidays),
        },
        channel=context.channel,
        request_id=context.request_id,
    )
    await session.refresh(calendar)
    return HolidayCalendarRead.model_validate(calendar)
