"""Aggregate payloads for the employee and manager dashboards."""

from __future__ import annotations

import uuid
from typing import List

from pydantic import Field

from app.schemas.common import ORMSchema
from app.schemas.holiday import UpcomingHoliday
from app.schemas.leave_balance import LeaveBalanceSummary
from app.schemas.leave_request import LeaveRequestListItem
from app.schemas.user import TeamMemberAway


class EmployeeDashboard(ORMSchema):
    """One round trip for the whole landing page."""

    user_id: uuid.UUID
    full_name: str
    year: int
    balances: List[LeaveBalanceSummary] = Field(default_factory=list)
    upcoming_holidays: List[UpcomingHoliday] = Field(default_factory=list)
    team_away: List[TeamMemberAway] = Field(default_factory=list)
    my_recent_requests: List[LeaveRequestListItem] = Field(default_factory=list)
    pending_approvals_count: int = 0


class TenantTenure(ORMSchema):
    """SaaS billing dashboard summary."""

    tenant_id: uuid.UUID
    name: str
    status: str
    subscription_tier: str
    employee_limit: int
    active_employees: int
    seats_remaining: int
