"""
Leave balance reads, locking and accrual synchronisation.

`lock_balance_for_update` is the single choke point through which every
deduction passes. Anything that changes `used_days` or `pending_days`
without going through it is a double-booking bug waiting to happen.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.enums import AuditAction
from app.models.leave_balance import LeaveBalance
from app.models.leave_policy import LeavePolicy
from app.models.leave_type import LeaveType
from app.models.user import User
from app.schemas.leave_balance import LeaveBalanceSummary
from app.services import audit_service, policy_service
from app.services.leave_calculator import (
    ZERO,
    AccrualRule,
    LeaveYear,
    accrued_days_as_of,
    quantize,
    rollover_expiry_date,
)


async def get_balance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
) -> Optional[LeaveBalance]:
    return await session.scalar(
        select(LeaveBalance).where(
            and_(
                LeaveBalance.tenant_id == tenant_id,
                LeaveBalance.user_id == user_id,
                LeaveBalance.leave_type_id == leave_type_id,
                LeaveBalance.year == year,
            )
        )
    )


async def get_or_create_balance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
    policy: Optional[LeavePolicy] = None,
) -> LeaveBalance:
    """
    Fetch the balance row, creating a zeroed one on first use.

    The unique constraint on (tenant, user, leave_type, year) means a
    concurrent creator loses with an IntegrityError, which the global handler
    turns into a retryable 409 rather than a duplicate row.
    """
    balance = await get_balance(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        leave_type_id=leave_type_id,
        year=year,
    )
    if balance is not None:
        return balance

    balance = LeaveBalance(
        tenant_id=tenant_id,
        user_id=user_id,
        leave_type_id=leave_type_id,
        year=year,
        policy_id=policy.id if policy else None,
    )
    session.add(balance)
    await session.flush()
    return balance


async def lock_balance_for_update(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
) -> LeaveBalance:
    """
    Re-read the balance row under a row-level write lock.

    This is the fix for the two-tabs-at-the-same-millisecond scenario: the
    second transaction blocks here until the first commits, then sees the
    already-reserved `pending_days` and fails the balance check. `lock_timeout`
    (set in `set_tenant_context`) keeps a wedged lock from hanging a worker.
    """
    balance = await session.scalar(
        select(LeaveBalance)
        .where(
            and_(
                LeaveBalance.tenant_id == tenant_id,
                LeaveBalance.user_id == user_id,
                LeaveBalance.leave_type_id == leave_type_id,
                LeaveBalance.year == year,
            )
        )
        .with_for_update()
    )
    if balance is None:
        raise NotFoundError("Leave balance not found for this leave type and year.")
    return balance


async def list_balances(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, year: int
) -> List[LeaveBalance]:
    stmt = select(LeaveBalance).where(
        and_(
            LeaveBalance.tenant_id == tenant_id,
            LeaveBalance.user_id == user_id,
            LeaveBalance.year == year,
        )
    )
    return list(await session.scalars(stmt))


async def summaries_for_user(
    session: AsyncSession, *, user: User, year: int
) -> List[LeaveBalanceSummary]:
    """Donut-chart payload: one entry per leave type the user holds."""
    balances = await list_balances(
        session, tenant_id=user.tenant_id, user_id=user.id, year=year
    )
    summaries: List[LeaveBalanceSummary] = []
    for balance in balances:
        leave_type: LeaveType = balance.leave_type
        summaries.append(
            LeaveBalanceSummary(
                leave_type_id=balance.leave_type_id,
                leave_type_code=leave_type.code,
                leave_type_name=leave_type.name,
                color_hex=leave_type.color_hex,
                year=balance.year,
                entitled_days=quantize(balance.entitled_days),
                used_days=quantize(balance.used_days),
                pending_days=quantize(balance.pending_days),
                available_days=quantize(balance.available_days),
                rolled_over_days=quantize(balance.rolled_over_days),
                rollover_expires_on=balance.rollover_expires_on,
            )
        )
    summaries.sort(key=lambda s: s.leave_type_name)
    return summaries


async def sync_accrual(
    session: AsyncSession,
    *,
    user: User,
    leave_type_id: uuid.UUID,
    leave_year: LeaveYear,
    as_of: date,
    policy: Optional[LeavePolicy] = None,
) -> LeaveBalance:
    """
    Bring `accrued_days` up to date for one user/leave type.

    Idempotent by design: the accrued figure is *recomputed* from the policy
    rather than incremented, so re-running the nightly job (or running it
    twice from two pods) can never double-credit anyone.
    """
    if policy is None:
        policy = await policy_service.resolve_policy(
            session,
            tenant_id=user.tenant_id,
            leave_type_id=leave_type_id,
            on_date=as_of,
            user=user,
        )

    balance = await get_or_create_balance(
        session,
        tenant_id=user.tenant_id,
        user_id=user.id,
        leave_type_id=leave_type_id,
        year=leave_year.key,
        policy=policy,
    )
    if policy is None:
        return balance

    accrued = accrued_days_as_of(
        AccrualRule.from_policy(policy),
        user.date_of_joining,
        as_of,
        leave_year,
        exit_date=user.date_of_exit,
    )
    balance.accrued_days = accrued
    balance.policy_id = policy.id
    balance.last_accrued_on = as_of
    if balance.rolled_over_days > 0 and balance.rollover_expires_on is None:
        balance.rollover_expires_on = rollover_expiry_date(
            leave_year, policy.rollover_expiry_months
        )
    return balance


async def expire_stale_rollover(
    session: AsyncSession, *, balance: LeaveBalance, today: date
) -> Decimal:
    """
    Lapse carried-over days whose window has closed.

    Only the portion that is still unspent lapses — days already consumed
    from the carry-over were legitimately used before the deadline.
    """
    if balance.rollover_expires_on is None or today <= balance.rollover_expires_on:
        return ZERO
    if balance.rolled_over_days <= 0:
        return ZERO

    unused_rollover = min(balance.rolled_over_days, max(balance.available_days, ZERO))
    if unused_rollover <= 0:
        return ZERO

    balance.expired_days = quantize(balance.expired_days + unused_rollover)
    balance.rollover_expires_on = None
    return quantize(unused_rollover)


async def adjust_balance(
    session: AsyncSession,
    *,
    actor: User,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
    delta_days: Decimal,
    reason: str,
    request_id: Optional[str] = None,
) -> LeaveBalance:
    """HR manual correction. Always locked, always audited."""
    balance = await lock_balance_for_update(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        leave_type_id=leave_type_id,
        year=year,
    )
    before = balance.to_dict()
    balance.opening_days = quantize(balance.opening_days + Decimal(delta_days))

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_balance",
        entity_id=balance.id,
        actor_id=actor.id,
        actor_email=actor.email,
        before=before,
        after={**balance.to_dict(), "adjustment_reason": reason},
        request_id=request_id,
    )
    return balance
