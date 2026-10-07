"""
Leave request lifecycle: estimate, submit, cancel, query.

The submit path is the most safety-critical flow in the product. Its order
of operations is deliberate:

  1. validate everything cheap and stateless first (dates, policy rules);
  2. take the row lock on the balance;
  3. re-read the balance *inside* the lock and decide;
  4. reserve the days as `pending_days`;
  5. write the request and its approval chain.

Doing the balance check before the lock — the intuitive order — is exactly
the double-booking bug the spec calls out: two tabs both read "1 day left",
both pass, both insert.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.context import RequestContext
from app.core.exceptions import (
    InsufficientBalanceError,
    InvalidStateTransitionError,
    NotFoundError,
    OverlappingLeaveError,
    PermissionDeniedError,
    PolicyViolationError,
    ValidationFailedError,
)
from app.models.enums import AuditAction, DayPart, LeaveStatus, LeaveUnit, UserRole
from app.models.leave_policy import LeavePolicy
from app.models.leave_request import LeaveRequest
from app.models.leave_type import LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave_request import (
    ConflictWarning,
    LeaveEstimateRequest,
    LeaveEstimateResponse,
    LeaveRequestCreate,
)
from app.services import (
    approval_service,
    audit_service,
    balance_service,
    calendar_service,
    notification_service,
    policy_service,
)
from app.services.leave_calculator import (
    ZERO,
    LeaveYear,
    WorkCalendar,
    hours_to_days,
    is_adjacent,
    local_today,
    local_window_to_utc,
    notice_period_days,
    overlapping_dates,
    quantize,
    resolve_leave_year,
    violates_clubbing_rule,
    working_day_breakdown,
)

# Statuses that still hold a claim on the calendar and the balance.
ACTIVE_STATUSES = (LeaveStatus.PENDING, LeaveStatus.APPROVED)


async def _load_leave_type(
    session: AsyncSession, tenant_id: uuid.UUID, leave_type_id: uuid.UUID
) -> LeaveType:
    leave_type = await session.get(LeaveType, leave_type_id)
    if leave_type is None or leave_type.tenant_id != tenant_id:
        raise NotFoundError("Leave type not found.")
    if not leave_type.is_active:
        raise ValidationFailedError("This leave type is no longer available.")
    return leave_type


async def _prepare(
    session: AsyncSession,
    *,
    user: User,
    tenant: Tenant,
    leave_type_id: uuid.UUID,
    start: date,
    end: date,
) -> Tuple[LeaveType, WorkCalendar, LeaveYear, Optional[LeavePolicy]]:
    """Shared context for both estimate and submit, so they can't diverge."""
    leave_type = await _load_leave_type(session, user.tenant_id, leave_type_id)
    work_calendar = await calendar_service.work_calendar_for_user(
        session, user, years=sorted({start.year, end.year})
    )
    leave_year = resolve_leave_year(start, tenant.fiscal_year_start_month)
    policy = await policy_service.resolve_policy(
        session,
        tenant_id=user.tenant_id,
        leave_type_id=leave_type_id,
        on_date=start,
        user=user,
    )
    return leave_type, work_calendar, leave_year, policy


def _validate_against_leave_type(
    *,
    leave_type: LeaveType,
    duration_days: Decimal,
    start: date,
    end: date,
    start_day_part: DayPart,
    end_day_part: DayPart,
    attachment_url: Optional[str],
    duration_hours: Optional[Decimal],
    today: date,
    work_calendar: WorkCalendar,
) -> List[str]:
    """
    Enforce the intrinsic rules of the leave type.

    Returns non-blocking warnings; blocking problems raise.
    """
    warnings: List[str] = []

    if duration_days <= 0:
        raise ValidationFailedError(
            "The selected dates contain no working days — they are all weekends "
            "or public holidays at your location."
        )

    is_half = DayPart.FULL_DAY not in (start_day_part, end_day_part)
    if is_half and not leave_type.allow_half_day:
        raise PolicyViolationError(f"{leave_type.name} cannot be taken as a half day.")

    if duration_hours is not None and (
        not leave_type.allow_hourly or leave_type.unit != LeaveUnit.HOUR
    ):
        raise PolicyViolationError(
            f"{leave_type.name} cannot be taken in hourly increments."
        )

    if (
        leave_type.max_consecutive_days is not None
        and duration_days > leave_type.max_consecutive_days
    ):
        raise PolicyViolationError(
            f"{leave_type.name} is limited to {leave_type.max_consecutive_days} "
            "consecutive days.",
            details={"max_consecutive_days": leave_type.max_consecutive_days},
        )

    threshold = leave_type.attachment_required_above_days
    if (
        threshold is not None
        and duration_days > Decimal(threshold)
        and not attachment_url
    ):
        raise PolicyViolationError(
            f"{leave_type.name} longer than {threshold} day(s) requires a supporting "
            "document (e.g. a medical certificate).",
            details={"attachment_required_above_days": str(threshold)},
        )

    if start < today:
        if not leave_type.allow_retroactive:
            raise PolicyViolationError(
                f"{leave_type.name} cannot be applied for retroactively.",
                details={"earliest_allowed": today.isoformat()},
            )
        warnings.append("This is a back-dated request and may need extra scrutiny.")
    elif leave_type.min_notice_days > 0:
        notice = notice_period_days(today, start, work_calendar)
        if notice < leave_type.min_notice_days:
            raise PolicyViolationError(
                f"{leave_type.name} requires {leave_type.min_notice_days} working "
                f"day(s) of notice; this request gives {notice}.",
                details={"required_notice_days": leave_type.min_notice_days},
            )

    return warnings


async def _find_overlapping(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    exclude_id: Optional[uuid.UUID] = None,
) -> List[LeaveRequest]:
    conditions = [
        LeaveRequest.tenant_id == tenant_id,
        LeaveRequest.user_id == user_id,
        LeaveRequest.status.in_(ACTIVE_STATUSES),
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    ]
    if exclude_id is not None:
        conditions.append(LeaveRequest.id != exclude_id)
    return list(await session.scalars(select(LeaveRequest).where(and_(*conditions))))


async def _check_clubbing(
    session: AsyncSession,
    *,
    user: User,
    leave_type: LeaveType,
    start: date,
    end: date,
    work_calendar: WorkCalendar,
) -> None:
    """
    Block forbidden combinations such as sick leave bolted onto annual leave.

    "Adjacent" ignores weekends and holidays, so Friday-annual plus
    Monday-sick is caught rather than waved through on a technicality.
    """
    if not leave_type.cannot_club_with:
        return

    # A week either side is plenty to find anything that could be "adjacent"
    # across a weekend or a holiday bridge.
    window_start = start - timedelta(days=7)
    window_end = end + timedelta(days=7)
    neighbours = await session.scalars(
        select(LeaveRequest).where(
            and_(
                LeaveRequest.tenant_id == user.tenant_id,
                LeaveRequest.user_id == user.id,
                LeaveRequest.status.in_(ACTIVE_STATUSES),
                LeaveRequest.start_date <= window_end,
                LeaveRequest.end_date >= window_start,
            )
        )
    )

    adjacent_codes: List[str] = []
    for neighbour in neighbours:
        touching = is_adjacent(neighbour.end_date, start, work_calendar) or is_adjacent(
            end, neighbour.start_date, work_calendar
        )
        if touching:
            adjacent_codes.append(neighbour.leave_type.code)

    if violates_clubbing_rule(
        leave_type.code, leave_type.cannot_club_with, adjacent_codes
    ):
        raise PolicyViolationError(
            f"{leave_type.name} cannot be combined with adjacent "
            f"{', '.join(sorted(set(adjacent_codes)))} leave.",
            details={"adjacent_leave_types": sorted(set(adjacent_codes))},
        )


async def _detect_conflicts(
    session: AsyncSession, *, user: User, start: date, end: date
) -> Optional[ConflictWarning]:
    """
    "3 other people in your department are on leave during these dates."

    Advisory only — understaffing is a judgement call for the manager, not
    something the system should refuse outright.
    """
    teammate_filter = (
        User.manager_id == user.manager_id
        if user.manager_id is not None
        else User.location_id == user.location_id
    )

    rows = await session.execute(
        select(
            User.first_name,
            User.last_name,
            LeaveRequest.start_date,
            LeaveRequest.end_date,
        )
        .join(LeaveRequest, LeaveRequest.user_id == User.id)
        .where(
            and_(
                User.tenant_id == user.tenant_id,
                User.id != user.id,
                User.is_active.is_(True),
                teammate_filter,
                LeaveRequest.status.in_(ACTIVE_STATUSES),
                LeaveRequest.start_date <= end,
                LeaveRequest.end_date >= start,
            )
        )
        .limit(25)
    )

    teammates: List[str] = []
    at_risk: set = set()
    for first_name, last_name, other_start, other_end in rows:
        teammates.append(f"{first_name} {last_name}".strip())
        at_risk.update(overlapping_dates(start, end, other_start, other_end))

    if not teammates:
        return None
    return ConflictWarning(
        overlapping_count=len(teammates),
        teammates=teammates,
        dates_at_risk=sorted(at_risk),
    )


# ── Estimate ───────────────────────────────────────────────────────────────


async def estimate(
    session: AsyncSession,
    *,
    user: User,
    tenant: Tenant,
    payload: LeaveEstimateRequest,
) -> LeaveEstimateResponse:
    """Price a request without committing to it (drives the live UI preview)."""
    leave_type, work_calendar, leave_year, policy = await _prepare(
        session,
        user=user,
        tenant=tenant,
        leave_type_id=payload.leave_type_id,
        start=payload.start_date,
        end=payload.end_date,
    )
    breakdown = working_day_breakdown(
        payload.start_date,
        payload.end_date,
        work_calendar,
        payload.start_day_part,
        payload.end_day_part,
    )

    balance = await balance_service.get_balance(
        session,
        tenant_id=user.tenant_id,
        user_id=user.id,
        leave_type_id=leave_type.id,
        year=leave_year.key,
    )
    available = quantize(balance.available_days) if balance else ZERO

    warnings: List[str] = []
    if breakdown.working_days > available:
        if policy is not None and policy.allow_negative_balance:
            warnings.append(
                "This exceeds your balance and will be recorded against your "
                f"borrowing limit of {policy.max_negative_days} day(s)."
            )
        else:
            warnings.append("This exceeds your available balance.")
    if breakdown.holiday_days:
        warnings.append(
            f"{breakdown.holiday_days} public holiday(s) in this range are not deducted."
        )

    conflicts = await _detect_conflicts(
        session, user=user, start=payload.start_date, end=payload.end_date
    )
    if conflicts and conflicts.overlapping_count >= settings.CONFLICT_WARNING_THRESHOLD:
        warnings.append(
            f"{conflicts.overlapping_count} teammate(s) are already away on these dates."
        )

    threshold = leave_type.attachment_required_above_days
    return LeaveEstimateResponse(
        working_days=breakdown.working_days,
        calendar_days=breakdown.calendar_days,
        weekend_days=breakdown.weekend_days,
        holiday_days=breakdown.holiday_days,
        holidays_excluded=list(breakdown.holidays_excluded),
        available_balance=available,
        balance_after=quantize(available - breakdown.working_days),
        requires_attachment=bool(
            threshold is not None and breakdown.working_days > Decimal(threshold)
        ),
        conflicts=conflicts,
        warnings=warnings,
    )


# ── Submit ─────────────────────────────────────────────────────────────────


async def create_leave_request(
    session: AsyncSession,
    *,
    user: User,
    tenant: Tenant,
    payload: LeaveRequestCreate,
    context: RequestContext,
) -> LeaveRequest:
    """Submit a leave request. See the module docstring for the lock ordering."""
    leave_type, work_calendar, leave_year, policy = await _prepare(
        session,
        user=user,
        tenant=tenant,
        leave_type_id=payload.leave_type_id,
        start=payload.start_date,
        end=payload.end_date,
    )

    # "Today" from the employee's own timezone: someone in Auckland must be
    # able to book same-day leave that is still yesterday in UTC.
    employee_today = local_today(user.effective_timezone)

    breakdown = working_day_breakdown(
        payload.start_date,
        payload.end_date,
        work_calendar,
        payload.start_day_part,
        payload.end_day_part,
    )
    duration_days = breakdown.working_days
    if payload.duration_hours is not None:
        duration_days = hours_to_days(payload.duration_hours, work_calendar)

    warnings = _validate_against_leave_type(
        leave_type=leave_type,
        duration_days=duration_days,
        start=payload.start_date,
        end=payload.end_date,
        start_day_part=payload.start_day_part,
        end_day_part=payload.end_day_part,
        attachment_url=payload.attachment_url,
        duration_hours=payload.duration_hours,
        today=employee_today,
        work_calendar=work_calendar,
    )

    existing = await _find_overlapping(
        session,
        user_id=user.id,
        tenant_id=user.tenant_id,
        start=payload.start_date,
        end=payload.end_date,
    )
    if existing:
        raise OverlappingLeaveError(
            "You already have a pending or approved request covering these dates.",
            details={"conflicting_request_ids": [str(r.id) for r in existing]},
        )

    await _check_clubbing(
        session,
        user=user,
        leave_type=leave_type,
        start=payload.start_date,
        end=payload.end_date,
        work_calendar=work_calendar,
    )

    conflicts = await _detect_conflicts(
        session, user=user, start=payload.start_date, end=payload.end_date
    )

    # Ensure the row exists before locking it — `SELECT ... FOR UPDATE` can
    # only lock rows that are already there.
    await balance_service.get_or_create_balance(
        session,
        tenant_id=user.tenant_id,
        user_id=user.id,
        leave_type_id=leave_type.id,
        year=leave_year.key,
        policy=policy,
    )

    # ── The critical section ────────────────────────────────────────────
    balance = await balance_service.lock_balance_for_update(
        session,
        tenant_id=user.tenant_id,
        user_id=user.id,
        leave_type_id=leave_type.id,
        year=leave_year.key,
    )
    available = Decimal(balance.available_days)
    if duration_days > available:
        allowed_overdraft = (
            Decimal(policy.max_negative_days)
            if policy is not None and policy.allow_negative_balance
            else ZERO
        )
        if duration_days > available + allowed_overdraft:
            raise InsufficientBalanceError(
                f"You have {available} day(s) of {leave_type.name} available but "
                f"requested {duration_days}.",
                details={
                    "available_days": str(available),
                    "requested_days": str(duration_days),
                    "overdraft_allowed": str(allowed_overdraft),
                },
            )

    balance.pending_days = quantize(Decimal(balance.pending_days) + duration_days)
    # ── End critical section ────────────────────────────────────────────

    starts_at_utc, ends_at_utc = local_window_to_utc(
        payload.start_date,
        payload.end_date,
        user.effective_timezone,
        payload.start_day_part,
        payload.end_day_part,
    )

    leave_request = LeaveRequest(
        tenant_id=user.tenant_id,
        user_id=user.id,
        leave_type_id=leave_type.id,
        policy_id=policy.id if policy else None,
        balance_year=leave_year.key,
        start_date=payload.start_date,
        end_date=payload.end_date,
        start_day_part=payload.start_day_part,
        end_day_part=payload.end_day_part,
        starts_at_utc=starts_at_utc,
        ends_at_utc=ends_at_utc,
        requester_timezone=user.effective_timezone,
        duration_days=duration_days,
        duration_hours=payload.duration_hours,
        status=LeaveStatus.PENDING,
        reason=payload.reason,
        attachment_url=payload.attachment_url,
        contact_number=payload.contact_number,
        is_retroactive=payload.start_date < employee_today,
        conflict_snapshot={
            "acknowledged": payload.acknowledged_conflicts,
            "overlapping_count": conflicts.overlapping_count if conflicts else 0,
            "teammates": conflicts.teammates if conflicts else [],
            "warnings": warnings,
        },
        submitted_at=datetime.now(timezone.utc),
    )
    session.add(leave_request)
    await session.flush()

    workflow = await approval_service.select_workflow(
        session,
        tenant_id=user.tenant_id,
        leave_type_id=leave_type.id,
        duration_days=duration_days,
        user=user,
    )
    leave_request.workflow_id = workflow.id if workflow else None
    await approval_service.build_approval_chain(
        session, leave_request=leave_request, requester=user, workflow=workflow
    )

    if not leave_type.requires_approval:
        # Auto-approved types (e.g. unpaid personal time in some tenants)
        # still move through the same balance transitions.
        balance.pending_days = quantize(
            max(Decimal(balance.pending_days) - duration_days, ZERO)
        )
        balance.used_days = quantize(Decimal(balance.used_days) + duration_days)
        leave_request.status = LeaveStatus.APPROVED
        leave_request.decided_at = datetime.now(timezone.utc)
    else:
        await _notify_submission(session, leave_request, user)

    await audit_service.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.SUBMIT,
        entity_type="leave_request",
        entity_id=leave_request.id,
        actor_id=user.id,
        actor_email=user.email,
        after=leave_request.to_dict(),
        channel=context.channel,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        request_id=context.request_id,
    )
    return leave_request


async def _notify_submission(
    session: AsyncSession, leave_request: LeaveRequest, requester: User
) -> None:
    approval = await approval_service.get_current_approval(session, leave_request)
    approver = (
        await session.get(User, approval.approver_id)
        if approval and approval.approver_id
        else None
    )
    notification_service.notify_request_submitted(
        tenant_id=leave_request.tenant_id,
        leave_request_id=leave_request.id,
        approver_id=approver.id if approver else None,
        approver_email=approver.email if approver else None,
        requester_name=requester.full_name,
        leave_type_name=leave_request.leave_type.name,
        start_date=leave_request.start_date.isoformat(),
        end_date=leave_request.end_date.isoformat(),
        duration_days=str(leave_request.duration_days),
    )


# ── Cancel ─────────────────────────────────────────────────────────────────


async def cancel_leave_request(
    session: AsyncSession,
    *,
    leave_request: LeaveRequest,
    actor: User,
    reason: Optional[str],
    context: RequestContext,
) -> LeaveRequest:
    """
    Cancel a pending or future approved request and release the days.

    Leave that has already been taken cannot be cancelled — that would
    retroactively hand back days the person was actually absent for. HR
    corrects those with an explicit balance adjustment instead.
    """
    if leave_request.status.is_terminal:
        raise InvalidStateTransitionError(
            f"This request is already {leave_request.status.value}."
        )

    is_owner = leave_request.user_id == actor.id
    if not is_owner and actor.role not in (UserRole.HR, UserRole.ADMIN):
        raise PermissionDeniedError("You can only cancel your own leave requests.")

    employee_today = local_today(leave_request.requester_timezone)
    if (
        leave_request.status == LeaveStatus.APPROVED
        and leave_request.start_date <= employee_today
        and actor.role not in (UserRole.HR, UserRole.ADMIN)
    ):
        raise InvalidStateTransitionError(
            "Leave that has already started cannot be cancelled. Ask HR for an "
            "adjustment."
        )

    before = leave_request.to_dict()
    balance = await balance_service.lock_balance_for_update(
        session,
        tenant_id=leave_request.tenant_id,
        user_id=leave_request.user_id,
        leave_type_id=leave_request.leave_type_id,
        year=leave_request.balance_year,
    )
    duration = Decimal(leave_request.duration_days)
    if leave_request.status == LeaveStatus.PENDING:
        balance.pending_days = quantize(
            max(Decimal(balance.pending_days) - duration, ZERO)
        )
    else:
        balance.used_days = quantize(max(Decimal(balance.used_days) - duration, ZERO))

    leave_request.status = LeaveStatus.CANCELLED
    leave_request.cancelled_at = datetime.now(timezone.utc)
    leave_request.decision_comment = reason

    await audit_service.record(
        session,
        tenant_id=leave_request.tenant_id,
        action=AuditAction.CANCEL,
        entity_type="leave_request",
        entity_id=leave_request.id,
        actor_id=actor.id,
        actor_email=actor.email,
        before=before,
        after=leave_request.to_dict(),
        channel=context.channel,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        request_id=context.request_id,
    )
    return leave_request


# ── Queries ────────────────────────────────────────────────────────────────


async def get_request_for_actor(
    session: AsyncSession, *, request_id: uuid.UUID, actor: User
) -> LeaveRequest:
    """
    Fetch one request, enforcing visibility.

    Visible to: the owner, their management chain, and HR/admins. RLS
    already prevents cross-tenant reads; this is the intra-tenant layer.
    """
    leave_request = await session.get(LeaveRequest, request_id)
    if leave_request is None or leave_request.tenant_id != actor.tenant_id:
        raise NotFoundError("Leave request not found.")

    if leave_request.user_id == actor.id or actor.role in (UserRole.HR, UserRole.ADMIN):
        return leave_request

    owner = await session.get(User, leave_request.user_id)
    if owner is not None and owner.manager_id == actor.id:
        return leave_request

    approver_ids = {approval.approver_id for approval in leave_request.approvals}
    if actor.id in approver_ids:
        return leave_request

    raise PermissionDeniedError("You do not have access to this leave request.")


async def list_requests(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ids: Optional[Sequence[uuid.UUID]] = None,
    statuses: Optional[Sequence[LeaveStatus]] = None,
    leave_type_id: Optional[uuid.UUID] = None,
    start_from: Optional[date] = None,
    start_to: Optional[date] = None,
    limit: int = 25,
    offset: int = 0,
) -> Tuple[List[LeaveRequest], int]:
    conditions = [LeaveRequest.tenant_id == tenant_id]
    if user_ids:
        conditions.append(LeaveRequest.user_id.in_(list(user_ids)))
    if statuses:
        conditions.append(LeaveRequest.status.in_(list(statuses)))
    if leave_type_id:
        conditions.append(LeaveRequest.leave_type_id == leave_type_id)
    if start_from:
        conditions.append(LeaveRequest.end_date >= start_from)
    if start_to:
        conditions.append(LeaveRequest.start_date <= start_to)

    total = await session.scalar(
        select(func.count(LeaveRequest.id)).where(and_(*conditions))
    )
    rows = await session.scalars(
        select(LeaveRequest)
        .where(and_(*conditions))
        .order_by(LeaveRequest.start_date.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)


async def team_away(
    session: AsyncSession, *, user: User, on_from: date, on_to: date
) -> List[Tuple[User, LeaveRequest]]:
    """'Who is away' widget: approved absences across the user's team."""
    teammate_filter = (
        or_(User.manager_id == user.manager_id, User.manager_id == user.id)
        if user.manager_id is not None
        else or_(User.manager_id == user.id, User.location_id == user.location_id)
    )
    rows = await session.execute(
        select(User, LeaveRequest)
        .join(LeaveRequest, LeaveRequest.user_id == User.id)
        .where(
            and_(
                User.tenant_id == user.tenant_id,
                User.id != user.id,
                teammate_filter,
                LeaveRequest.status == LeaveStatus.APPROVED,
                LeaveRequest.start_date <= on_to,
                LeaveRequest.end_date >= on_from,
            )
        )
        .order_by(LeaveRequest.start_date)
        .limit(50)
    )
    return [(row[0], row[1]) for row in rows]
