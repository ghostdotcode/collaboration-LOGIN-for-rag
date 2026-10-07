"""
HR configuration: leave types, versioned policies and the year-end
rollover dry run.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_tenant,
    get_current_user,
    get_request_context,
    require_hr,
)
from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.models.enums import AuditAction
from app.models.leave_balance import LeaveBalance
from app.models.leave_type import LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave_policy import (
    LeavePolicyCreate,
    LeavePolicyRead,
    YearEndSimulationRequest,
    YearEndSimulationResult,
    YearEndSimulationRow,
)
from app.schemas.leave_type import LeaveTypeCreate, LeaveTypeRead, LeaveTypeUpdate
from app.services import audit_service, balance_service, policy_service
from app.services.leave_calculator import (
    ZERO,
    compute_rollover,
    quantize,
    resolve_leave_year,
    rollover_expiry_date,
)

router = APIRouter(tags=["Policies & Leave Types"])


# ── Leave types ────────────────────────────────────────────────────────────


@router.get(
    "/leave-types", response_model=List[LeaveTypeRead], summary="List leave types"
)
async def list_leave_types(
    include_inactive: bool = Query(default=False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[LeaveTypeRead]:
    conditions = [LeaveType.tenant_id == user.tenant_id]
    if not include_inactive:
        conditions.append(LeaveType.is_active.is_(True))
    rows = await session.scalars(
        select(LeaveType).where(and_(*conditions)).order_by(LeaveType.name)
    )
    return [LeaveTypeRead.model_validate(row) for row in rows]


@router.post(
    "/leave-types",
    response_model=LeaveTypeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a leave type (HR)",
)
async def create_leave_type(
    payload: LeaveTypeCreate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveTypeRead:
    existing = await session.scalar(
        select(LeaveType.id).where(
            LeaveType.tenant_id == actor.tenant_id, LeaveType.code == payload.code
        )
    )
    if existing:
        raise ValidationFailedError(f"A leave type with code '{payload.code}' exists.")

    leave_type = LeaveType(tenant_id=actor.tenant_id, **payload.model_dump())
    session.add(leave_type)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor_id=actor.id,
        actor_email=actor.email,
        after=leave_type.to_dict(),
        channel=context.channel,
        request_id=context.request_id,
    )
    return LeaveTypeRead.model_validate(leave_type)


@router.patch(
    "/leave-types/{leave_type_id}",
    response_model=LeaveTypeRead,
    summary="Update a leave type (HR)",
)
async def update_leave_type(
    leave_type_id: uuid.UUID,
    payload: LeaveTypeUpdate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveTypeRead:
    leave_type = await session.get(LeaveType, leave_type_id)
    if leave_type is None or leave_type.tenant_id != actor.tenant_id:
        raise NotFoundError("Leave type not found.")

    before = leave_type.to_dict()
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(leave_type, field, value)

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor_id=actor.id,
        actor_email=actor.email,
        before=before,
        after=leave_type.to_dict(),
        channel=context.channel,
        request_id=context.request_id,
    )
    return LeaveTypeRead.model_validate(leave_type)


# ── Policies (immutable versions) ──────────────────────────────────────────


@router.get(
    "/policies",
    response_model=List[LeavePolicyRead],
    summary="Policy versions for a leave type",
)
async def list_policies(
    leave_type_id: uuid.UUID = Query(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[LeavePolicyRead]:
    rows = await policy_service.list_versions(
        session, tenant_id=user.tenant_id, leave_type_id=leave_type_id
    )
    return [LeavePolicyRead.model_validate(row) for row in rows]


@router.get(
    "/policies/effective",
    response_model=Optional[LeavePolicyRead],
    summary="The policy in force for me on a given date",
)
async def effective_policy(
    leave_type_id: uuid.UUID = Query(...),
    on_date: Optional[date] = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Optional[LeavePolicyRead]:
    policy = await policy_service.resolve_policy(
        session,
        tenant_id=user.tenant_id,
        leave_type_id=leave_type_id,
        on_date=on_date or datetime.now(timezone.utc).date(),
        user=user,
    )
    return LeavePolicyRead.model_validate(policy) if policy else None


@router.post(
    "/policies",
    response_model=LeavePolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new policy version (HR)",
)
async def create_policy(
    payload: LeavePolicyCreate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeavePolicyRead:
    """
    Publish a policy change as a new version.

    Nothing is overwritten: the previous version is closed with an
    `effective_to` date, so balances already computed under it keep their
    provenance. Cutting annual leave from 20 to 15 in October therefore does
    not retroactively rewrite January-September accruals.
    """
    policy = await policy_service.create_policy_version(
        session,
        tenant_id=actor.tenant_id,
        payload=payload,
        actor=actor,
        request_id=context.request_id,
    )
    return LeavePolicyRead.model_validate(policy)


# ── Year-end processing ────────────────────────────────────────────────────


@router.post(
    "/policies/year-end",
    response_model=YearEndSimulationResult,
    summary="Dry-run (or commit) year-end rollover (HR)",
)
async def year_end(
    payload: YearEndSimulationRequest,
    actor: User = Depends(require_hr),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> YearEndSimulationResult:
    """
    Simulate lapsing/rollover before touching anyone's balance.

    `commit=false` (the default) computes and returns the outcome without
    writing, which is what makes it safe to run repeatedly in December. With
    `commit=true` the same computation is applied and audited.
    """
    conditions = [
        LeaveBalance.tenant_id == actor.tenant_id,
        LeaveBalance.year == payload.year,
    ]
    if payload.leave_type_id is not None:
        conditions.append(LeaveBalance.leave_type_id == payload.leave_type_id)

    balances = list(
        await session.scalars(select(LeaveBalance).where(and_(*conditions)))
    )

    rows: List[YearEndSimulationRow] = []
    total_rolled = ZERO
    total_lapsed = ZERO
    next_year = resolve_leave_year(
        date(payload.year + 1, tenant.fiscal_year_start_month, 1),
        tenant.fiscal_year_start_month,
    )

    for balance in balances:
        owner = await session.get(User, balance.user_id)
        if owner is None or not owner.is_active:
            continue

        policy = await policy_service.resolve_policy(
            session,
            tenant_id=actor.tenant_id,
            leave_type_id=balance.leave_type_id,
            on_date=next_year.start,
            user=owner,
        )
        max_rollover = Decimal(policy.max_rollover_days) if policy else ZERO
        allow_encashment = bool(policy.allow_encashment) if policy else False

        closing = Decimal(balance.available_days)
        outcome = compute_rollover(
            closing,
            max_rollover,
            encashable_days=ZERO,
            allow_encashment=allow_encashment,
        )
        total_rolled += outcome.rolled_over
        total_lapsed += outcome.lapsed

        rows.append(
            YearEndSimulationRow(
                user_id=owner.id,
                full_name=owner.full_name,
                leave_type=balance.leave_type.name,
                closing_balance=quantize(closing),
                rolled_over=outcome.rolled_over,
                lapsed=outcome.lapsed,
                encashed=outcome.encashed,
            )
        )

        if payload.commit:
            next_balance = await balance_service.get_or_create_balance(
                session,
                tenant_id=actor.tenant_id,
                user_id=owner.id,
                leave_type_id=balance.leave_type_id,
                year=next_year.key,
                policy=policy,
            )
            next_balance.rolled_over_days = outcome.rolled_over
            next_balance.rollover_expires_on = rollover_expiry_date(
                next_year, policy.rollover_expiry_months if policy else 0
            )
            balance.expired_days = quantize(balance.expired_days + outcome.lapsed)
            balance.encashed_days = quantize(balance.encashed_days + outcome.encashed)

    if payload.commit:
        await audit_service.record(
            session,
            tenant_id=actor.tenant_id,
            action=AuditAction.ROLLOVER,
            entity_type="leave_balance",
            actor_id=actor.id,
            actor_email=actor.email,
            after={
                "year": payload.year,
                "employees_processed": len(rows),
                "total_rolled_over": str(quantize(total_rolled)),
                "total_lapsed": str(quantize(total_lapsed)),
            },
            channel=context.channel,
            request_id=context.request_id,
        )

    return YearEndSimulationResult(
        year=payload.year,
        committed=payload.commit,
        employees_processed=len(rows),
        total_rolled_over=quantize(total_rolled),
        total_lapsed=quantize(total_lapsed),
        rows=rows,
    )
