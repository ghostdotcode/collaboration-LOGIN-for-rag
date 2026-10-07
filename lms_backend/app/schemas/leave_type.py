"""Leave type schemas (HR configuration surface)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from app.models.enums import LeaveUnit
from app.schemas.common import ORMSchema, SafeCode, SafeLine, SafeText, StrictSchema


class LeaveTypeBase(StrictSchema):
    code: SafeCode
    name: SafeLine
    description: Optional[SafeText] = None
    unit: LeaveUnit = LeaveUnit.DAY
    is_paid: bool = True
    requires_approval: bool = True
    allow_half_day: bool = True
    allow_hourly: bool = False
    attachment_required_above_days: Optional[Decimal] = Field(
        default=None, ge=0, le=365, decimal_places=2
    )
    max_consecutive_days: Optional[int] = Field(default=None, ge=1, le=365)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    allow_retroactive: bool = False
    cannot_club_with: List[SafeCode] = Field(default_factory=list, max_length=32)
    eligibility_rules: Dict[str, object] = Field(default_factory=dict)
    color_hex: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class LeaveTypeCreate(LeaveTypeBase):
    pass


class LeaveTypeUpdate(StrictSchema):
    name: Optional[SafeLine] = None
    description: Optional[SafeText] = None
    requires_approval: Optional[bool] = None
    allow_half_day: Optional[bool] = None
    allow_hourly: Optional[bool] = None
    attachment_required_above_days: Optional[Decimal] = Field(
        default=None, ge=0, le=365
    )
    max_consecutive_days: Optional[int] = Field(default=None, ge=1, le=365)
    min_notice_days: Optional[int] = Field(default=None, ge=0, le=365)
    allow_retroactive: Optional[bool] = None
    cannot_club_with: Optional[List[SafeCode]] = None
    color_hex: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    is_active: Optional[bool] = None


class LeaveTypeRead(ORMSchema):
    id: uuid.UUID
    code: str
    name: str
    description: Optional[str]
    unit: LeaveUnit
    is_paid: bool
    requires_approval: bool
    allow_half_day: bool
    allow_hourly: bool
    attachment_required_above_days: Optional[Decimal]
    max_consecutive_days: Optional[int]
    min_notice_days: int
    allow_retroactive: bool
    cannot_club_with: List[str]
    color_hex: str
    is_active: bool
