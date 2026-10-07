"""
Approval chain construction and decision handling.

Two pieces of care worth calling out:
  * Delegation is resolved *at decision time*, so a manager who assigns a
    stand-in after requests are already queued doesn't strand them.
  * Every terminal decision moves days between `pending_days` and
    `used_days` under the same row lock used at submission, so approvals can
    never leave the balance inconsistent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional, Sequence, Set

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.context import RequestContext
from app.core.exceptions import (
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from app.models.approval_workflow import (
    ApprovalWorkflow,
    ApprovalWorkflowStep,
    LeaveApproval,
)
from app.models.delegation import Delegation
from app.models.enums import (
    ApprovalStatus,
    ApproverType,
    AuditAction,
    LeaveStatus,
    UserRole,
)
from app.models.leave_request import LeaveRequest
from app.models.user import User
from app.schemas.approval import BulkApprovalResponse, BulkApprovalResultRow
from app.services import audit_service, balance_service, notification_service
from app.services.leave_calculator import ZERO, quantize

# ── Workflow selection ─────────────────────────────────────────────────────


async def select_workflow(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    duration_days: Decimal,
    user: User,
) -> Optional[ApprovalWorkflow]:
    """
    Pick the most specific active workflow for this request.

    Specificity order: leave-type match > location match > role match >
    highest duration threshold. A tenant-wide default (all selectors null)
    is the fallback.
    """
    stmt = (
        select(ApprovalWorkflow)
        .where(
            and_(
                ApprovalWorkflow.tenant_id == tenant_id,
                ApprovalWorkflow.is_active.is_(True),
                ApprovalWorkflow.min_duration_days <= duration_days,
                or_(
                    ApprovalWorkflow.leave_type_id.is_(None),
                    ApprovalWorkflow.leave_type_id == leave_type_id,
                ),
                or_(
                    ApprovalWorkflow.applies_to_location_id.is_(None),
                    ApprovalWorkflow.applies_to_location_id == user.location_id,
                ),
                or_(
                    ApprovalWorkflow.applies_to_role.is_(None),
                    ApprovalWorkflow.applies_to_role == user.role,
                ),
            )
        )
        .options(selectinload(ApprovalWorkflow.steps))
        .order_by(
            ApprovalWorkflow.leave_type_id.is_(None).asc(),
            ApprovalWorkflow.applies_to_location_id.is_(None).asc(),
            ApprovalWorkflow.applies_to_role.is_(None).asc(),
            ApprovalWorkflow.min_duration_days.desc(),
        )
        .limit(1)
    )
    return await session.scalar(stmt)


async def _resolve_step_approver(
    session: AsyncSession, step: ApprovalWorkflowStep, requester: User
) -> Optional[uuid.UUID]:
    """Turn an abstract step ("HR") into a concrete user id."""
    if step.approver_type == ApproverType.SPECIFIC_USER:
        return step.approver_user_id

    if step.approver_type == ApproverType.REPORTING_MANAGER:
        return requester.manager_id

    if step.approver_type == ApproverType.SKIP_LEVEL_MANAGER:
        if requester.manager_id is None:
            return None
        manager = await session.get(User, requester.manager_id)
        return manager.manager_id if manager else None

    target_role = (
        UserRole.HR if step.approver_type == ApproverType.HR else step.approver_role
    )
    if target_role is None:
        return None
    # Any active holder of the role; ordering by email keeps it deterministic
    # so re-running chain construction yields the same approver.
    return await session.scalar(
        select(User.id)
        .where(
            and_(
                User.tenant_id == requester.tenant_id,
                User.role == target_role,
                User.is_active.is_(True),
                User.id != requester.id,
            )
        )
        .order_by(User.email)
        .limit(1)
    )


async def build_approval_chain(
    session: AsyncSession,
    *,
    leave_request: LeaveRequest,
    requester: User,
    workflow: Optional[ApprovalWorkflow],
) -> List[LeaveApproval]:
    """
    Materialise the approval rows for a request.

    Steps that resolve to nobody (no manager on file, no HR user yet) or to
    the requester themselves are recorded as SKIPPED rather than dropped, so
    the audit trail still shows the chain that was intended.
    """
    approvals: List[LeaveApproval] = []
    steps: Sequence[ApprovalWorkflowStep] = (
        sorted(workflow.steps, key=lambda s: s.level) if workflow else []
    )

    if not steps:
        # No configured workflow: fall back to the reporting manager, or HR
        # if the requester has no manager (e.g. the CEO's own request).
        approver_id = requester.manager_id
        if approver_id is None:
            approver_id = await session.scalar(
                select(User.id)
                .where(
                    User.tenant_id == requester.tenant_id,
                    User.role.in_([UserRole.HR, UserRole.ADMIN]),
                    User.is_active.is_(True),
                    User.id != requester.id,
                )
                .order_by(User.email)
                .limit(1)
            )
        approvals.append(
            LeaveApproval(
                tenant_id=requester.tenant_id,
                leave_request_id=leave_request.id,
                level=1,
                approver_id=approver_id,
                status=ApprovalStatus.PENDING
                if approver_id
                else ApprovalStatus.SKIPPED,
            )
        )
    else:
        for step in steps:
            approver_id = await _resolve_step_approver(session, step, requester)
            if approver_id == requester.id:
                approver_id = None  # nobody approves their own leave
            approvals.append(
                LeaveApproval(
                    tenant_id=requester.tenant_id,
                    leave_request_id=leave_request.id,
                    level=step.level,
                    approver_id=approver_id,
                    status=ApprovalStatus.PENDING
                    if approver_id
                    else ApprovalStatus.SKIPPED,
                )
            )

    for approval in approvals:
        session.add(approval)

    first_actionable = next(
        (a.level for a in approvals if a.status == ApprovalStatus.PENDING), None
    )
    leave_request.current_level = first_actionable or (
        approvals[-1].level if approvals else 1
    )
    await session.flush()
    return approvals


# ── Delegation ─────────────────────────────────────────────────────────────


async def delegated_principals(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delegate_id: uuid.UUID,
    on_date: date,
) -> Set[uuid.UUID]:
    """Users whose approvals `delegate_id` may act on today."""
    rows = await session.scalars(
        select(Delegation.delegator_id).where(
            and_(
                Delegation.tenant_id == tenant_id,
                Delegation.delegate_id == delegate_id,
                Delegation.is_active.is_(True),
                Delegation.starts_on <= on_date,
                Delegation.ends_on >= on_date,
            )
        )
    )
    return set(rows)


# ── Queries ────────────────────────────────────────────────────────────────


async def pending_for_approver(
    session: AsyncSession,
    *,
    approver: User,
    on_date: date,
    limit: int = 50,
    offset: int = 0,
) -> List[LeaveRequest]:
    """The approver's queue, including anything delegated to them."""
    principals = await delegated_principals(
        session,
        tenant_id=approver.tenant_id,
        delegate_id=approver.id,
        on_date=on_date,
    )
    actionable_ids = {approver.id, *principals}

    stmt = (
        select(LeaveRequest)
        .join(LeaveApproval, LeaveApproval.leave_request_id == LeaveRequest.id)
        .where(
            and_(
                LeaveRequest.tenant_id == approver.tenant_id,
                LeaveRequest.status == LeaveStatus.PENDING,
                LeaveApproval.level == LeaveRequest.current_level,
                LeaveApproval.status == ApprovalStatus.PENDING,
                LeaveApproval.approver_id.in_(actionable_ids),
            )
        )
        .order_by(LeaveRequest.start_date)
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(stmt))


async def get_current_approval(
    session: AsyncSession, leave_request: LeaveRequest
) -> Optional[LeaveApproval]:
    return await session.scalar(
        select(LeaveApproval).where(
            and_(
                LeaveApproval.leave_request_id == leave_request.id,
                LeaveApproval.level == leave_request.current_level,
            )
        )
    )


# ── Decisions ──────────────────────────────────────────────────────────────


async def decide(
    session: AsyncSession,
    *,
    leave_request: LeaveRequest,
    actor: User,
    approve: bool,
    comment: Optional[str],
    context: RequestContext,
    today: Optional[date] = None,
) -> LeaveRequest:
    """
    Record one approval-chain decision and advance (or finalise) the request.

    Authorisation accepts three principals: the assigned approver, an active
    delegate of that approver, or a tenant admin acting as an override — the
    last of which is always recorded with the admin's identity in the ledger.
    """
    if leave_request.status != LeaveStatus.PENDING:
        raise InvalidStateTransitionError(
            f"This request is already {leave_request.status.value}."
        )

    approval = await get_current_approval(session, leave_request)
    if approval is None or approval.status != ApprovalStatus.PENDING:
        raise InvalidStateTransitionError("There is no pending approval at this level.")

    reference_day = today or datetime.now(timezone.utc).date()
    delegated_from: Optional[uuid.UUID] = None

    if approval.approver_id == actor.id:
        pass
    else:
        principals = await delegated_principals(
            session,
            tenant_id=actor.tenant_id,
            delegate_id=actor.id,
            on_date=reference_day,
        )
        if approval.approver_id in principals or actor.role == UserRole.ADMIN:
            delegated_from = approval.approver_id
        else:
            raise PermissionDeniedError("This request is not awaiting your approval.")

    before = leave_request.to_dict()

    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    approval.approver_id = actor.id
    approval.delegated_from_id = delegated_from
    approval.comment = comment
    approval.decided_at = datetime.now(timezone.utc)
    approval.channel = context.channel
    approval.ip_address = context.ip_address
    approval.user_agent = (context.user_agent or "")[:256] or None

    if not approve:
        await _finalise_rejection(session, leave_request, comment)
    else:
        next_level = await _next_pending_level(session, leave_request)
        if next_level is None:
            await _finalise_approval(session, leave_request, comment)
        else:
            leave_request.current_level = next_level
            await _notify_next_approver(session, leave_request)

    await audit_service.record(
        session,
        tenant_id=leave_request.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
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


async def _next_pending_level(
    session: AsyncSession, leave_request: LeaveRequest
) -> Optional[int]:
    return await session.scalar(
        select(LeaveApproval.level)
        .where(
            and_(
                LeaveApproval.leave_request_id == leave_request.id,
                LeaveApproval.level > leave_request.current_level,
                LeaveApproval.status == ApprovalStatus.PENDING,
            )
        )
        .order_by(LeaveApproval.level)
        .limit(1)
    )


async def _finalise_approval(
    session: AsyncSession, leave_request: LeaveRequest, comment: Optional[str]
) -> None:
    """Convert the reservation into consumption under a row lock."""
    balance = await balance_service.lock_balance_for_update(
        session,
        tenant_id=leave_request.tenant_id,
        user_id=leave_request.user_id,
        leave_type_id=leave_request.leave_type_id,
        year=leave_request.balance_year,
    )
    duration = Decimal(leave_request.duration_days)
    balance.pending_days = quantize(max(balance.pending_days - duration, ZERO))
    balance.used_days = quantize(balance.used_days + duration)

    leave_request.status = LeaveStatus.APPROVED
    leave_request.decided_at = datetime.now(timezone.utc)
    leave_request.decision_comment = comment
    await _notify_requester(session, leave_request, "approved", comment)


async def _finalise_rejection(
    session: AsyncSession, leave_request: LeaveRequest, comment: Optional[str]
) -> None:
    """Release the reservation and skip any downstream levels."""
    balance = await balance_service.lock_balance_for_update(
        session,
        tenant_id=leave_request.tenant_id,
        user_id=leave_request.user_id,
        leave_type_id=leave_request.leave_type_id,
        year=leave_request.balance_year,
    )
    duration = Decimal(leave_request.duration_days)
    balance.pending_days = quantize(max(balance.pending_days - duration, ZERO))

    downstream = await session.scalars(
        select(LeaveApproval).where(
            and_(
                LeaveApproval.leave_request_id == leave_request.id,
                LeaveApproval.level > leave_request.current_level,
                LeaveApproval.status == ApprovalStatus.PENDING,
            )
        )
    )
    for approval in downstream:
        approval.status = ApprovalStatus.SKIPPED

    leave_request.status = LeaveStatus.REJECTED
    leave_request.decided_at = datetime.now(timezone.utc)
    leave_request.decision_comment = comment
    await _notify_requester(session, leave_request, "rejected", comment)


async def _notify_requester(
    session: AsyncSession,
    leave_request: LeaveRequest,
    decision: str,
    comment: Optional[str],
) -> None:
    requester = await session.get(User, leave_request.user_id)
    if requester is None:
        return
    notification_service.notify_decision(
        tenant_id=leave_request.tenant_id,
        leave_request_id=leave_request.id,
        to_email=requester.email,
        decision=decision,
        decided_by="your approver",
        comment=comment,
        start_date=leave_request.start_date.isoformat(),
        end_date=leave_request.end_date.isoformat(),
    )


async def _notify_next_approver(
    session: AsyncSession, leave_request: LeaveRequest
) -> None:
    approval = await get_current_approval(session, leave_request)
    if approval is None or approval.approver_id is None:
        return
    approver = await session.get(User, approval.approver_id)
    requester = await session.get(User, leave_request.user_id)
    if approver is None or requester is None:
        return
    notification_service.notify_request_submitted(
        tenant_id=leave_request.tenant_id,
        leave_request_id=leave_request.id,
        approver_id=approver.id,
        approver_email=approver.email,
        requester_name=requester.full_name,
        leave_type_name=leave_request.leave_type.name,
        start_date=leave_request.start_date.isoformat(),
        end_date=leave_request.end_date.isoformat(),
        duration_days=str(leave_request.duration_days),
    )


async def bulk_decide(
    session: AsyncSession,
    *,
    actor: User,
    request_ids: Sequence[uuid.UUID],
    approve: bool,
    comment: Optional[str],
    context: RequestContext,
) -> BulkApprovalResponse:
    """
    Approve/reject many requests in one call.

    Each decision runs in its own SAVEPOINT so one bad row (already
    cancelled, not yours, balance gone) doesn't roll back the other 99.
    """
    results: List[BulkApprovalResultRow] = []

    for request_id in request_ids:
        try:
            async with session.begin_nested():
                leave_request = await session.get(LeaveRequest, request_id)
                if leave_request is None or leave_request.tenant_id != actor.tenant_id:
                    raise NotFoundError("Leave request not found.")
                await decide(
                    session,
                    leave_request=leave_request,
                    actor=actor,
                    approve=approve,
                    comment=comment,
                    context=context,
                )
            results.append(BulkApprovalResultRow(request_id=request_id, succeeded=True))
        except Exception as exc:
            code = getattr(exc, "code", "internal_error")
            message = getattr(exc, "message", str(exc))
            results.append(
                BulkApprovalResultRow(
                    request_id=request_id,
                    succeeded=False,
                    error_code=code,
                    message=message,
                )
            )

    succeeded = sum(1 for row in results if row.succeeded)
    return BulkApprovalResponse(
        processed=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )
