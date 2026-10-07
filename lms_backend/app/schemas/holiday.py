"""Holiday calendar schemas."""

from __future__ import annotations

import uuid
from datetime import date
from typing import List, Optional

from pydantic import Field

from app.schemas.common import ORMSchema, SafeLine, StrictSchema


class HolidayCreate(StrictSchema):
    name: SafeLine
    holiday_date: date
    is_optional: bool = False


class HolidayRead(ORMSchema):
    id: uuid.UUID
    name: str
    holiday_date: date
    is_optional: bool


class HolidayCalendarCreate(StrictSchema):
    name: SafeLine
    year: int = Field(ge=1970, le=2200)
    country_code: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    location_id: Optional[uuid.UUID] = None
    holidays: List[HolidayCreate] = Field(default_factory=list, max_length=100)


class HolidayCalendarRead(ORMSchema):
    id: uuid.UUID
    name: str
    year: int
    country_code: Optional[str]
    location_id: Optional[uuid.UUID]
    is_active: bool
    holidays: List[HolidayRead] = Field(default_factory=list)


class UpcomingHoliday(ORMSchema):
    """Dashboard widget row."""

    name: str
    holiday_date: date
    days_away: int
    is_optional: bool
