"""Dashboard aggregation — one request powers the whole landing page."""

from __future__ import annotations

from datetime import timedelta
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_current_tenant, get_current_user
from app.api.dependencies.database import get_db
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.dashboard import EmployeeDashboard, TenantTenure
from app.schemas.holiday import UpcomingHoliday
from app.schemas.leave_request import LeaveRequestListItem
from app.schemas.user import TeamMemberAway
from app.services import (
    approval_service,
    balance_service,
    calendar_service,
    leave_request_service,
)
from app.services.leave_calculator import local_today, resolve_leave_year

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# How far ahead the "who is away" widget looks.
TEAM_AWAY_HORIZON_DAYS = 30


@router.get("/me", response_model=EmployeeDashboard, summary="My dashboard")
async def my_dashboard(
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> EmployeeDashboard:
    """
    Balances, upcoming holidays, who's away and my recent requests.

    Batched deliberately: the dashboard is the most-hit endpoint at 9am on a
    Monday, and five parallel round trips per user is how you fail the P99
    latency budget.
    """
    today = local_today(user.effective_timezone)
    leave_year = resolve_leave_year(today, tenant.fiscal_year_start_month)

    balances = await balance_service.summaries_for_user(
        session, user=user, year=leave_year.key
    )

    holidays = await calendar_service.upcoming_holidays(
        session, user, from_date=today, limit=5
    )
    upcoming = [
        UpcomingHoliday(
            name=holiday.name,
            holiday_date=holiday.holiday_date,
            days_away=(holiday.holiday_date - today).days,
            is_optional=holiday.is_optional,
        )
        for holiday in holidays
    ]

    away_rows = await leave_request_service.team_away(
        session,
        user=user,
        on_from=today,
        on_to=today + timedelta(days=TEAM_AWAY_HORIZON_DAYS),
    )
    team_away: List[TeamMemberAway] = [
        TeamMemberAway(
            user_id=teammate.id,
            full_name=teammate.full_name,
            leave_type=leave.leave_type.name,
            start_date=leave.start_date,
            end_date=leave.end_date,
        )
        for teammate, leave in away_rows
    ]

    recent, _ = await leave_request_service.list_requests(
        session, tenant_id=user.tenant_id, user_ids=[user.id], limit=5, offset=0
    )

    pending_count = 0
    if user.role in (UserRole.MANAGER, UserRole.HR, UserRole.ADMIN):
        pending = await approval_service.pending_for_approver(
            session, approver=user, on_date=today, limit=100
        )
        pending_count = len(pending)

    return EmployeeDashboard(
        user_id=user.id,
        full_name=user.full_name,
        year=leave_year.key,
        balances=balances,
        upcoming_holidays=upcoming,
        team_away=team_away,
        my_recent_requests=[LeaveRequestListItem.model_validate(row) for row in recent],
        pending_approvals_count=pending_count,
    )


@router.get(
    "/tenant",
    response_model=TenantTenure,
    summary="Workspace subscription usage (admin)",
)
async def tenant_overview(
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> TenantTenure:
    from sqlalchemy import func, select

    active = await session.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == tenant.id, User.is_active.is_(True)
        )
    )
    active_count = int(active or 0)
    return TenantTenure(
        tenant_id=tenant.id,
        name=tenant.name,
        status=tenant.status.value,
        subscription_tier=tenant.subscription_tier,
        employee_limit=tenant.employee_limit,
        active_employees=active_count,
        seats_remaining=max(tenant.employee_limit - active_count, 0),
    )
