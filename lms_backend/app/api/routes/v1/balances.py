"""Leave balance endpoints (dashboard donuts + HR corrections)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_tenant,
    get_current_user,
    get_request_context,
    require_hr,
)
from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave_balance import (
    BalanceAdjustment,
    BalanceOverview,
    LeaveBalanceSummary,
)
from app.services import balance_service
from app.services.leave_calculator import resolve_leave_year

router = APIRouter(prefix="/balances", tags=["Leave Balances"])


def _current_year(tenant: Tenant, year: Optional[int]) -> int:
    if year is not None:
        return year
    return resolve_leave_year(
        datetime.now(timezone.utc).date(), tenant.fiscal_year_start_month
    ).key


@router.get("/me", response_model=BalanceOverview, summary="My leave balances")
async def my_balances(
    year: Optional[int] = Query(default=None, ge=1970, le=2200),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> BalanceOverview:
    resolved_year = _current_year(tenant, year)
    summaries = await balance_service.summaries_for_user(
        session, user=user, year=resolved_year
    )
    return BalanceOverview(user_id=user.id, year=resolved_year, balances=summaries)


@router.get(
    "/{user_id}",
    response_model=BalanceOverview,
    summary="An employee's balances (manager/HR)",
)
async def user_balances(
    user_id: uuid.UUID,
    year: Optional[int] = Query(default=None, ge=1970, le=2200),
    actor: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> BalanceOverview:
    target = await session.get(User, user_id)
    if target is None or target.tenant_id != actor.tenant_id:
        raise NotFoundError("Employee not found.")

    is_self = target.id == actor.id
    is_their_manager = target.manager_id == actor.id
    if not (is_self or is_their_manager or actor.role in (UserRole.HR, UserRole.ADMIN)):
        raise PermissionDeniedError("You cannot view this employee's balances.")

    resolved_year = _current_year(tenant, year)
    summaries = await balance_service.summaries_for_user(
        session, user=target, year=resolved_year
    )
    return BalanceOverview(user_id=target.id, year=resolved_year, balances=summaries)


@router.post(
    "/adjust",
    response_model=List[LeaveBalanceSummary],
    summary="Manually correct a balance (HR)",
)
async def adjust(
    payload: BalanceAdjustment,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> List[LeaveBalanceSummary]:
    """
    Apply a signed correction to an employee's opening balance.

    Corrections are never silent: the row is locked, the delta and reason go
    into the audit ledger, and the adjusted figure is returned immediately.
    """
    target = await session.get(User, payload.user_id)
    if target is None or target.tenant_id != actor.tenant_id:
        raise NotFoundError("Employee not found.")

    await balance_service.adjust_balance(
        session,
        actor=actor,
        tenant_id=actor.tenant_id,
        user_id=payload.user_id,
        leave_type_id=payload.leave_type_id,
        year=payload.year,
        delta_days=Decimal(payload.delta_days),
        reason=payload.reason,
        request_id=context.request_id,
    )
    return await balance_service.summaries_for_user(
        session, user=target, year=payload.year
    )
