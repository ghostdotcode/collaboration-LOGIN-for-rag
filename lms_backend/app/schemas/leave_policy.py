"""
Leave policy schemas.

Note what `LeavePolicyCreate` deliberately omits: `version`, `effective_to`
and `previous_version_id`. Clients never choose a version number — the
service assigns it so the immutable chain can't be forged or reordered.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, model_validator

from app.models.enums import AccrualFrequency, UserRole
from app.schemas.common import ORMSchema, SafeLine, StrictSchema


class LeavePolicyCreate(StrictSchema):
    leave_type_id: uuid.UUID
    name: SafeLine
    effective_from: date
    annual_quota_days: Decimal = Field(ge=0, le=365, decimal_places=2)
    accrual_frequency: AccrualFrequency = AccrualFrequency.MONTHLY
    accrual_rate_days: Decimal = Field(
        default=Decimal("0"), ge=0, le=31, decimal_places=2
    )
    prorate_on_joining: bool = True
    accrual_starts_after_days: int = Field(default=0, ge=0, le=730)
    max_rollover_days: Decimal = Field(default=Decimal("0"), ge=0, le=365)
    rollover_expiry_months: int = Field(default=0, ge=0, le=24)
    allow_encashment: bool = False
    allow_negative_balance: bool = False
    max_negative_days: Decimal = Field(default=Decimal("0"), ge=0, le=60)
    applies_to_role: Optional[UserRole] = None
    applies_to_location_id: Optional[uuid.UUID] = None
    min_tenure_months: int = Field(default=0, ge=0, le=480)

    @model_validator(mode="after")
    def _validate_consistency(self) -> "LeavePolicyCreate":
        if (
            self.accrual_frequency != AccrualFrequency.NONE
            and self.accrual_rate_days == 0
        ):
            raise ValueError(
                "accrual_rate_days must be > 0 unless accrual_frequency is 'none'"
            )
        if self.allow_negative_balance and self.max_negative_days <= 0:
            raise ValueError(
                "max_negative_days must be > 0 when allow_negative_balance is true"
            )
        if self.max_rollover_days > self.annual_quota_days:
            raise ValueError("max_rollover_days cannot exceed annual_quota_days")
        return self


class LeavePolicyRead(ORMSchema):
    id: uuid.UUID
    leave_type_id: uuid.UUID
    name: str
    version: int
    effective_from: date
    effective_to: Optional[date]
    previous_version_id: Optional[uuid.UUID]
    annual_quota_days: Decimal
    accrual_frequency: AccrualFrequency
    accrual_rate_days: Decimal
    prorate_on_joining: bool
    accrual_starts_after_days: int
    max_rollover_days: Decimal
    rollover_expiry_months: int
    allow_encashment: bool
    allow_negative_balance: bool
    max_negative_days: Decimal
    applies_to_role: Optional[UserRole]
    applies_to_location_id: Optional[uuid.UUID]
    min_tenure_months: int


class YearEndSimulationRequest(StrictSchema):
    """Dry-run the year-end rollover/lapse before committing anything."""

    year: int = Field(ge=1970, le=2200)
    leave_type_id: Optional[uuid.UUID] = None
    commit: bool = False


class YearEndSimulationRow(ORMSchema):
    user_id: uuid.UUID
    full_name: str
    leave_type: str
    closing_balance: Decimal
    rolled_over: Decimal
    lapsed: Decimal
    encashed: Decimal


class YearEndSimulationResult(ORMSchema):
    year: int
    committed: bool
    employees_processed: int
    total_rolled_over: Decimal
    total_lapsed: Decimal
    rows: List[YearEndSimulationRow]
