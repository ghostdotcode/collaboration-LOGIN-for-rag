"""
Leave request schemas.

`duration_days` is never accepted from the client — the server recomputes it
from the dates, the employee's working week and their holiday calendar.
Trusting a client-supplied duration would let anyone book a fortnight while
debiting half a day.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, field_validator, model_validator

from app.models.enums import ActionChannel, ApprovalStatus, DayPart, LeaveStatus
from app.schemas.common import (
    ORMSchema,
    PhoneNumber,
    SafeText,
    StrictSchema,
)
from app.schemas.user import UserBrief

# A single request spanning more than a year is almost always a typo; long
# absences (sabbatical, parental) are modelled as separate requests.
MAX_REQUEST_SPAN_DAYS = 366


class _DateRangeMixin(StrictSchema):
    start_date: date
    end_date: date
    start_day_part: DayPart = DayPart.FULL_DAY
    end_day_part: DayPart = DayPart.FULL_DAY

    @model_validator(mode="after")
    def _validate_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        if (self.end_date - self.start_date).days > MAX_REQUEST_SPAN_DAYS:
            raise ValueError(
                f"A single request may not span more than {MAX_REQUEST_SPAN_DAYS} days"
            )
        if self.start_date == self.end_date:
            # One calendar day: the two day-part fields describe the same day,
            # so contradicting halves ("first half" -> "second half") are
            # nonsense and would double-count the deduction.
            if self.start_day_part != self.end_day_part and DayPart.FULL_DAY not in (
                self.start_day_part,
                self.end_day_part,
            ):
                raise ValueError(
                    "For a single-day request, start_day_part and end_day_part must match"
                )
        else:
            # Mid-range days are always full days, so only a *trailing* part of
            # the first day and a *leading* part of the last day make sense.
            if self.start_day_part == DayPart.FIRST_HALF:
                raise ValueError(
                    "start_day_part cannot be 'first_half' on a multi-day request; "
                    "use 'second_half' to start after lunch"
                )
            if self.end_day_part == DayPart.SECOND_HALF:
                raise ValueError(
                    "end_day_part cannot be 'second_half' on a multi-day request; "
                    "use 'first_half' to return after lunch"
                )
        return self


class LeaveRequestCreate(_DateRangeMixin):
    leave_type_id: uuid.UUID
    reason: Optional[SafeText] = Field(default=None, max_length=1000)
    # Set only for hourly leave types.
    duration_hours: Optional[Decimal] = Field(
        default=None, gt=0, le=24, decimal_places=2
    )
    attachment_url: Optional[str] = Field(default=None, max_length=512)
    contact_number: Optional[PhoneNumber] = None
    # Client sends the conflicts it displayed, so we can record that the
    # employee was warned before submitting.
    acknowledged_conflicts: bool = False

    @field_validator("attachment_url")
    @classmethod
    def _https_only(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.startswith("https://"):
            raise ValueError("attachment_url must be an https URL")
        return v


class LeaveEstimateRequest(_DateRangeMixin):
    """Preview the cost of a request before submitting it."""

    leave_type_id: uuid.UUID


class ConflictWarning(ORMSchema):
    """'3 other people in your department are on leave during these dates.'"""

    overlapping_count: int
    teammates: List[str]
    dates_at_risk: List[date]


class LeaveEstimateResponse(ORMSchema):
    working_days: Decimal
    calendar_days: int
    weekend_days: int
    holiday_days: int
    holidays_excluded: List[date]
    available_balance: Decimal
    balance_after: Decimal
    requires_attachment: bool
    conflicts: Optional[ConflictWarning] = None
    warnings: List[str] = Field(default_factory=list)


class LeaveRequestCancel(StrictSchema):
    reason: Optional[SafeText] = Field(default=None, max_length=500)


class ApprovalRead(ORMSchema):
    id: uuid.UUID
    level: int
    status: ApprovalStatus
    approver: Optional[UserBrief]
    delegated_from_id: Optional[uuid.UUID]
    comment: Optional[str]
    decided_at: Optional[datetime]
    channel: ActionChannel


class LeaveRequestRead(ORMSchema):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    leave_type_id: uuid.UUID
    status: LeaveStatus
    start_date: date
    end_date: date
    start_day_part: DayPart
    end_day_part: DayPart
    duration_days: Decimal
    duration_hours: Optional[Decimal]
    reason: Optional[str]
    attachment_url: Optional[str]
    contact_number: Optional[str]
    requester_timezone: str
    balance_year: int
    current_level: int
    is_retroactive: bool
    conflict_snapshot: dict
    submitted_at: Optional[datetime]
    decided_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    decision_comment: Optional[str]
    created_at: datetime
    user: Optional[UserBrief] = None
    approvals: List[ApprovalRead] = Field(default_factory=list)


class LeaveRequestListItem(ORMSchema):
    """Lean row for tables and calendar views."""

    id: uuid.UUID
    user_id: uuid.UUID
    leave_type_id: uuid.UUID
    status: LeaveStatus
    start_date: date
    end_date: date
    duration_days: Decimal
    submitted_at: Optional[datetime]


class AttachmentPresignRequest(StrictSchema):
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[\w .\-()]+$")
    content_type: str = Field(pattern=r"^(image/(png|jpe?g|webp)|application/pdf)$")
    size_bytes: int = Field(gt=0)


class AttachmentPresignResponse(ORMSchema):
    upload_url: str
    object_key: str
    expires_in: int
    max_size_bytes: int
