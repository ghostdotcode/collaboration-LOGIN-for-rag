"""Leave request endpoints — the employee-facing core of the product."""

from __future__ import annotations

import uuid
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_tenant,
    get_current_user,
    get_request_context,
)
from app.api.dependencies.database import get_db
from app.api.dependencies.rate_limit import write_rate_limit
from app.core.config import settings
from app.core.context import RequestContext
from app.core.exceptions import ValidationFailedError
from app.core.security import generate_url_safe_token
from app.models.enums import LeaveStatus, UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.common import Page
from app.schemas.leave_request import (
    AttachmentPresignRequest,
    AttachmentPresignResponse,
    LeaveEstimateRequest,
    LeaveEstimateResponse,
    LeaveRequestCancel,
    LeaveRequestCreate,
    LeaveRequestListItem,
    LeaveRequestRead,
)
from app.services import leave_request_service

router = APIRouter(prefix="/leaves", tags=["Leave Requests"])


@router.post(
    "/estimate",
    response_model=LeaveEstimateResponse,
    summary="Preview the cost of a leave request",
)
async def estimate_leave(
    payload: LeaveEstimateRequest,
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> LeaveEstimateResponse:
    """
    Live preview for the apply form: working days, excluded holidays,
    resulting balance and any teammate clashes — before anything is written.
    """
    return await leave_request_service.estimate(
        session, user=user, tenant=tenant, payload=payload
    )


@router.post(
    "",
    response_model=LeaveRequestRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(write_rate_limit)],
    summary="Submit a leave request",
)
async def create_leave(
    payload: LeaveRequestCreate,
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveRequestRead:
    leave_request = await leave_request_service.create_leave_request(
        session, user=user, tenant=tenant, payload=payload, context=context
    )
    await session.flush()
    # The response carries the requester and the freshly built approval chain,
    # neither of which is loaded on a just-inserted row. Refresh here, where
    # awaiting is allowed, rather than letting serialisation try to lazy-load.
    await session.refresh(leave_request)
    return LeaveRequestRead.model_validate(leave_request)


@router.get(
    "", response_model=Page[LeaveRequestListItem], summary="List leave requests"
)
async def list_leaves(
    mine: bool = Query(default=True, description="Restrict to your own requests"),
    user_id: Optional[uuid.UUID] = Query(default=None),
    request_status: Optional[List[LeaveStatus]] = Query(default=None, alias="status"),
    leave_type_id: Optional[uuid.UUID] = Query(default=None),
    start_from: Optional[date] = Query(default=None),
    start_to: Optional[date] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Page[LeaveRequestListItem]:
    """
    Employees see their own history; managers and HR may scope to a person.

    Anyone asking for someone else's requests without the role for it is
    quietly narrowed to their own rather than erroring, so the UI can share
    one call across roles.
    """
    target_ids: Optional[List[uuid.UUID]] = [user.id]
    if not mine and user.role in (UserRole.MANAGER, UserRole.HR, UserRole.ADMIN):
        target_ids = [user_id] if user_id else None
    elif user_id and user_id != user.id and user.role in (UserRole.HR, UserRole.ADMIN):
        target_ids = [user_id]

    rows, total = await leave_request_service.list_requests(
        session,
        tenant_id=user.tenant_id,
        user_ids=target_ids,
        statuses=request_status,
        leave_type_id=leave_type_id,
        start_from=start_from,
        start_to=start_to,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return Page[LeaveRequestListItem](
        items=[LeaveRequestListItem.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{request_id}", response_model=LeaveRequestRead, summary="Get one request")
async def get_leave(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> LeaveRequestRead:
    leave_request = await leave_request_service.get_request_for_actor(
        session, request_id=request_id, actor=user
    )
    return LeaveRequestRead.model_validate(leave_request)


@router.post(
    "/{request_id}/cancel",
    response_model=LeaveRequestRead,
    dependencies=[Depends(write_rate_limit)],
    summary="Cancel a leave request",
)
async def cancel_leave(
    request_id: uuid.UUID,
    payload: LeaveRequestCancel,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> LeaveRequestRead:
    leave_request = await leave_request_service.get_request_for_actor(
        session, request_id=request_id, actor=user
    )
    updated = await leave_request_service.cancel_leave_request(
        session,
        leave_request=leave_request,
        actor=user,
        reason=payload.reason,
        context=context,
    )
    return LeaveRequestRead.model_validate(updated)


@router.post(
    "/attachments/presign",
    response_model=AttachmentPresignResponse,
    dependencies=[Depends(write_rate_limit)],
    summary="Get a pre-signed upload URL for a supporting document",
)
async def presign_attachment(
    payload: AttachmentPresignRequest,
    user: User = Depends(get_current_user),
) -> AttachmentPresignResponse:
    """
    Hand the browser a short-lived S3 URL so medical certificates never
    transit the API.

    The object key is tenant- and user-prefixed with a random component, so
    keys are neither guessable nor able to collide across tenants.
    """
    max_bytes = settings.MAX_ATTACHMENT_MB * 1024 * 1024
    if payload.size_bytes > max_bytes:
        raise ValidationFailedError(
            f"Attachments are limited to {settings.MAX_ATTACHMENT_MB} MB.",
            details={"max_size_bytes": max_bytes},
        )
    if not settings.S3_BUCKET:
        raise ValidationFailedError(
            "Document uploads are not configured for this deployment."
        )

    object_key = (
        f"tenants/{user.tenant_id}/users/{user.id}/"
        f"{generate_url_safe_token(12)}-{payload.filename}"
    )

    # boto3 is imported lazily so the dependency is only needed by
    # deployments that actually enable attachments.
    import boto3  # type: ignore[import-not-found]

    client = boto3.client("s3", region_name=settings.S3_REGION)
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.S3_BUCKET,
            "Key": object_key,
            "ContentType": payload.content_type,
        },
        ExpiresIn=settings.S3_PRESIGN_EXPIRY_SECONDS,
    )
    return AttachmentPresignResponse(
        upload_url=upload_url,
        object_key=object_key,
        expires_in=settings.S3_PRESIGN_EXPIRY_SECONDS,
        max_size_bytes=max_bytes,
    )
