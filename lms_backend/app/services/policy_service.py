"""
Leave policy resolution and immutable versioning.

The rule this module exists to enforce: **policy rows are never mutated.**
HR "editing" a policy actually appends a new version with an
`effective_from` date and closes the previous one, so a balance computed in
March under 20-days-a-year keeps its provenance after October's cut to 15.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationFailedError
from app.models.enums import AuditAction
from app.models.leave_policy import LeavePolicy
from app.models.leave_type import LeaveType
from app.models.user import User
from app.schemas.leave_policy import LeavePolicyCreate
from app.services import audit_service


async def resolve_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    on_date: date,
    user: Optional[User] = None,
) -> Optional[LeavePolicy]:
    """
    The policy version in force for this user on this date.

    When several versions match, the most *specific* one wins: a policy
    pinned to the user's location or role beats a tenant-wide default.
    """
    conditions = [
        LeavePolicy.tenant_id == tenant_id,
        LeavePolicy.leave_type_id == leave_type_id,
        LeavePolicy.effective_from <= on_date,
        or_(LeavePolicy.effective_to.is_(None), LeavePolicy.effective_to >= on_date),
    ]

    if user is not None:
        conditions.append(
            or_(
                LeavePolicy.applies_to_location_id.is_(None),
                LeavePolicy.applies_to_location_id == user.location_id,
            )
        )
        conditions.append(
            or_(
                LeavePolicy.applies_to_role.is_(None),
                LeavePolicy.applies_to_role == user.role,
            )
        )
        tenure_months = _tenure_months(user.date_of_joining, on_date)
        conditions.append(LeavePolicy.min_tenure_months <= tenure_months)
    else:
        conditions.append(LeavePolicy.applies_to_location_id.is_(None))
        conditions.append(LeavePolicy.applies_to_role.is_(None))

    stmt = (
        select(LeavePolicy)
        .where(and_(*conditions))
        .order_by(
            # NULLS LAST on the selectors == prefer the specific policy.
            LeavePolicy.applies_to_location_id.is_(None).asc(),
            LeavePolicy.applies_to_role.is_(None).asc(),
            LeavePolicy.min_tenure_months.desc(),
            LeavePolicy.effective_from.desc(),
            LeavePolicy.version.desc(),
        )
        .limit(1)
    )
    return await session.scalar(stmt)


def _tenure_months(joining_date: date, on_date: date) -> int:
    if on_date < joining_date:
        return 0
    months = (on_date.year - joining_date.year) * 12 + (
        on_date.month - joining_date.month
    )
    if on_date.day < joining_date.day:
        months -= 1
    return max(months, 0)


async def list_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, leave_type_id: uuid.UUID
) -> List[LeavePolicy]:
    stmt = (
        select(LeavePolicy)
        .where(
            LeavePolicy.tenant_id == tenant_id,
            LeavePolicy.leave_type_id == leave_type_id,
        )
        .order_by(LeavePolicy.version.desc())
    )
    return list(await session.scalars(stmt))


async def create_policy_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: LeavePolicyCreate,
    actor: User,
    request_id: Optional[str] = None,
) -> LeavePolicy:
    """
    Append a new policy version.

    Closes the currently-open version by stamping its `effective_to` — the
    single field we allow ourselves to write on an existing row, because it
    records when the version stopped applying rather than changing what it
    said while it did.
    """
    leave_type = await session.get(LeaveType, payload.leave_type_id)
    if leave_type is None or leave_type.tenant_id != tenant_id:
        raise NotFoundError("Leave type not found.")

    # Lock the version chain so two concurrent HR edits can't both claim the
    # same version number (the unique constraint would reject one anyway,
    # but this turns a 500-shaped race into an orderly queue).
    current = await session.scalar(
        select(LeavePolicy)
        .where(
            LeavePolicy.tenant_id == tenant_id,
            LeavePolicy.leave_type_id == payload.leave_type_id,
        )
        .order_by(LeavePolicy.version.desc())
        .limit(1)
        .with_for_update()
    )

    if current is not None:
        if payload.effective_from <= current.effective_from:
            raise ValidationFailedError(
                "effective_from must be after the current version's effective_from "
                f"({current.effective_from.isoformat()}).",
                details={"current_effective_from": current.effective_from.isoformat()},
            )
        current.effective_to = payload.effective_from - timedelta(days=1)

    next_version = (current.version + 1) if current else 1
    policy = LeavePolicy(
        tenant_id=tenant_id,
        leave_type_id=payload.leave_type_id,
        name=payload.name,
        version=next_version,
        effective_from=payload.effective_from,
        effective_to=None,
        previous_version_id=current.id if current else None,
        annual_quota_days=payload.annual_quota_days,
        accrual_frequency=payload.accrual_frequency,
        accrual_rate_days=payload.accrual_rate_days,
        prorate_on_joining=payload.prorate_on_joining,
        accrual_starts_after_days=payload.accrual_starts_after_days,
        max_rollover_days=payload.max_rollover_days,
        rollover_expiry_months=payload.rollover_expiry_months,
        allow_encashment=payload.allow_encashment,
        allow_negative_balance=payload.allow_negative_balance,
        max_negative_days=payload.max_negative_days,
        applies_to_role=payload.applies_to_role,
        applies_to_location_id=payload.applies_to_location_id,
        min_tenure_months=payload.min_tenure_months,
    )
    session.add(policy)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.POLICY_VERSION,
        entity_type="leave_policy",
        entity_id=policy.id,
        actor_id=actor.id,
        actor_email=actor.email,
        before=current.to_dict() if current else None,
        after=policy.to_dict(),
        request_id=request_id,
    )
    return policy


async def count_active_policies(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count(LeavePolicy.id)).where(
                LeavePolicy.tenant_id == tenant_id,
                LeavePolicy.effective_to.is_(None),
            )
        )
    ) or 0
