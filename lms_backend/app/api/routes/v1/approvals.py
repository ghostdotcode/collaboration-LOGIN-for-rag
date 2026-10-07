"""
Approval endpoints: the manager queue, bulk actions, one-click email
approvals and delegation management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_user,
    get_request_context,
    require_manager,
)
from app.api.dependencies.database import get_db
from app.api.dependencies.rate_limit import write_rate_limit
from app.core.context import RequestContext
from app.core.exceptions import LMSError, NotFoundError, PermissionDeniedError
from app.core.security import TokenType, decode_token
from app.db.session import set_tenant_context
from app.models.delegation import Delegation
from app.models.enums import ActionChannel, AuditAction
from app.models.leave_request import LeaveRequest
from app.models.user import User
from app.schemas.approval import (
    ApprovalDecisionRequest,
    BulkApprovalRequest,
    BulkApprovalResponse,
    DelegationCreate,
    DelegationRead,
    RejectionRequest,
)
from app.schemas.leave_request import LeaveRequestRead
from app.services import approval_service, audit_service, leave_request_service

router = APIRouter(prefix="/approvals", tags=["Approvals"])


@router.get(
    "/pending",
    response_model=List[LeaveRequestRead],
    summary="Requests awaiting my decision",
)
async def pending(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_manager),
    session: AsyncSession = Depends(get_db),
) -> List[LeaveRequestRead]:
    """Includes anything delegated to me while another manager is away."""
    rows = await approval_service.pending_for_approver(
        session,
        approver=user,
        on_date=datetime.now(timezone.utc).date(),
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return [LeaveRequestRead.model_validate(row) for row in rows]


@router.post(
    "/{request_id}/approve",
    response_model=LeaveRequestRead,
    dependencies=[Depends(write_rate_limit)],
    summary="Approve a request",
)
async def approve(
    request_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    user: User = Depends(require_manager),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveRequestRead:
    leave_request = await leave_request_service.get_request_for_actor(
        session, request_id=request_id, actor=user
    )
    updated = await approval_service.decide(
        session,
        leave_request=leave_request,
        actor=user,
        approve=True,
        comment=payload.comment,
        context=context,
    )
    return LeaveRequestRead.model_validate(updated)


@router.post(
    "/{request_id}/reject",
    response_model=LeaveRequestRead,
    dependencies=[Depends(write_rate_limit)],
    summary="Reject a request",
)
async def reject(
    request_id: uuid.UUID,
    payload: RejectionRequest,
    user: User = Depends(require_manager),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveRequestRead:
    leave_request = await leave_request_service.get_request_for_actor(
        session, request_id=request_id, actor=user
    )
    updated = await approval_service.decide(
        session,
        leave_request=leave_request,
        actor=user,
        approve=False,
        comment=payload.comment,
        context=context,
    )
    return LeaveRequestRead.model_validate(updated)


@router.post(
    "/bulk",
    response_model=BulkApprovalResponse,
    dependencies=[Depends(write_rate_limit)],
    summary="Approve or reject many requests at once",
)
async def bulk(
    payload: BulkApprovalRequest,
    user: User = Depends(require_manager),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> BulkApprovalResponse:
    """Each row succeeds or fails independently (per-request SAVEPOINT)."""
    return await approval_service.bulk_decide(
        session,
        actor=user,
        request_ids=payload.request_ids,
        approve=payload.decision == "approve",
        comment=payload.comment,
        context=context,
    )


@router.get(
    "/email-action",
    response_class=HTMLResponse,
    summary="One-click approve/reject from an email",
)
async def email_action(
    request: Request,
    token: str = Query(min_length=16),
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """
    Act on a request straight from the notification email.

    The token is single-action and single-request (see `create_action_token`),
    short-lived, and the decision is recorded with `channel=email` plus the
    caller's IP so the audit trail distinguishes it from a web approval.
    """
    claims = decode_token(token, expected_type=TokenType.ACTION)
    tenant_id = uuid.UUID(str(claims["tid"]))
    approver_id = uuid.UUID(str(claims["sub"]))
    leave_request_id = uuid.UUID(str(claims["rid"]))
    action = str(claims["act"])

    await set_tenant_context(session, tenant_id)
    approver = await session.get(User, approver_id)
    leave_request = await session.get(LeaveRequest, leave_request_id)
    if (
        approver is None
        or leave_request is None
        or leave_request.tenant_id != tenant_id
    ):
        raise NotFoundError("This request no longer exists.")

    forwarded = request.headers.get("X-Forwarded-For", "")
    context = RequestContext(
        ip_address=forwarded.split(",")[0].strip()
        or (request.client.host if request.client else None),
        user_agent=request.headers.get("User-Agent"),
        channel=ActionChannel.EMAIL,
    )

    try:
        await approval_service.decide(
            session,
            leave_request=leave_request,
            actor=approver,
            approve=action == "approve",
            comment=f"Actioned via email by {approver.email}",
            context=context,
        )
    except LMSError as exc:
        return HTMLResponse(
            _result_page("Could not complete", exc.message), status_code=exc.status_code
        )

    verb = "approved" if action == "approve" else "rejected"
    return HTMLResponse(
        _result_page(
            f"Request {verb}",
            f"The leave request for {leave_request.start_date} – "
            f"{leave_request.end_date} has been {verb}. You can close this tab.",
        )
    )


def _result_page(title: str, message: str) -> str:
    """Minimal self-contained page — email clients open this in a bare tab."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0e1120; color:#e2e8f0;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .card {{ background:#151829; border:1px solid rgba(255,255,255,.08);
           border-radius:16px; padding:32px 40px; max-width:460px; text-align:center; }}
  h1 {{ font-size:20px; margin:0 0 10px; }}
  p {{ color:#8892aa; font-size:14px; line-height:1.6; margin:0; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


# ── Delegation ─────────────────────────────────────────────────────────────


@router.post(
    "/delegations",
    response_model=DelegationRead,
    dependencies=[Depends(write_rate_limit)],
    summary="Assign a temporary approver",
)
async def create_delegation(
    payload: DelegationCreate,
    user: User = Depends(require_manager),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> DelegationRead:
    """Hand my approval authority to a colleague while I'm away."""
    if payload.delegate_id == user.id:
        raise PermissionDeniedError("You cannot delegate to yourself.")

    delegate = await session.get(User, payload.delegate_id)
    if (
        delegate is None
        or delegate.tenant_id != user.tenant_id
        or not delegate.is_active
    ):
        raise NotFoundError("The nominated delegate was not found.")

    delegation = Delegation(
        tenant_id=user.tenant_id,
        delegator_id=user.id,
        delegate_id=payload.delegate_id,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        reason=payload.reason,
    )
    session.add(delegation)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELEGATE,
        entity_type="delegation",
        entity_id=delegation.id,
        actor_id=user.id,
        actor_email=user.email,
        after=delegation.to_dict(),
        channel=context.channel,
        ip_address=context.ip_address,
        request_id=context.request_id,
    )
    return DelegationRead.model_validate(delegation)


@router.get(
    "/delegations",
    response_model=List[DelegationRead],
    summary="My delegations",
)
async def list_delegations(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[DelegationRead]:
    rows = await session.scalars(
        select(Delegation)
        .where(
            and_(
                Delegation.tenant_id == user.tenant_id,
                (Delegation.delegator_id == user.id)
                | (Delegation.delegate_id == user.id),
            )
        )
        .order_by(Delegation.starts_on.desc())
    )
    return [DelegationRead.model_validate(row) for row in rows]


@router.delete(
    "/delegations/{delegation_id}",
    response_model=DelegationRead,
    summary="Revoke a delegation",
)
async def revoke_delegation(
    delegation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DelegationRead:
    delegation = await session.get(Delegation, delegation_id)
    if delegation is None or delegation.tenant_id != user.tenant_id:
        raise NotFoundError("Delegation not found.")
    if delegation.delegator_id != user.id:
        raise PermissionDeniedError("Only the delegator can revoke a delegation.")
    delegation.is_active = False
    return DelegationRead.model_validate(delegation)
