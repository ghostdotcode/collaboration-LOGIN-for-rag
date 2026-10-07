"""Leave balance schemas — drives the dashboard donut charts."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional

from app.schemas.common import ORMSchema


class LeaveBalanceRead(ORMSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int
    opening_days: Decimal
    accrued_days: Decimal
    rolled_over_days: Decimal
    used_days: Decimal
    pending_days: Decimal
    encashed_days: Decimal
    expired_days: Decimal
    last_accrued_on: Optional[date]
    rollover_expires_on: Optional[date]


class LeaveBalanceSummary(ORMSchema):
    """One donut chart's worth of data."""

    leave_type_id: uuid.UUID
    leave_type_code: str
    leave_type_name: str
    color_hex: str
    year: int
    entitled_days: Decimal
    used_days: Decimal
    pending_days: Decimal
    available_days: Decimal
    rolled_over_days: Decimal
    rollover_expires_on: Optional[date]


class BalanceAdjustment(ORMSchema):
    """HR manual correction — always lands in the audit log."""

    leave_type_id: uuid.UUID
    user_id: uuid.UUID
    year: int
    delta_days: Decimal
    reason: str


class BalanceOverview(ORMSchema):
    user_id: uuid.UUID
    year: int
    balances: List[LeaveBalanceSummary]
