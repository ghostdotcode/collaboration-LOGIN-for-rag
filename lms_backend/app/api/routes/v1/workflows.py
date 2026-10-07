"""Custom approval-chain templates (HR drag-and-drop builder backend)."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.auth import get_current_user, get_request_context, require_hr
from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.models.approval_workflow import ApprovalWorkflow, ApprovalWorkflowStep
from app.models.enums import AuditAction
from app.models.user import User
from app.schemas.approval import WorkflowCreate, WorkflowRead
from app.services import audit_service

router = APIRouter(prefix="/workflows", tags=["Approval Workflows"])


@router.get("", response_model=List[WorkflowRead], summary="List approval workflows")
async def list_workflows(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[WorkflowRead]:
    rows = await session.scalars(
        select(ApprovalWorkflow)
        .where(ApprovalWorkflow.tenant_id == user.tenant_id)
        .options(selectinload(ApprovalWorkflow.steps))
        .order_by(ApprovalWorkflow.name)
    )
    return [WorkflowRead.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an approval workflow (HR)",
)
async def create_workflow(
    payload: WorkflowCreate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> WorkflowRead:
    """
    Define a chain such as L1 Manager -> L2 Manager -> HR.

    Step levels are validated as consecutive from 1 by the schema, so a
    saved chain can never contain a gap that would stall a request forever.
    """
    duplicate = await session.scalar(
        select(ApprovalWorkflow.id).where(
            ApprovalWorkflow.tenant_id == actor.tenant_id,
            ApprovalWorkflow.name == payload.name,
        )
    )
    if duplicate:
        raise ValidationFailedError("A workflow with this name already exists.")

    workflow = ApprovalWorkflow(
        tenant_id=actor.tenant_id,
        name=payload.name,
        leave_type_id=payload.leave_type_id,
        applies_to_location_id=payload.applies_to_location_id,
        applies_to_role=payload.applies_to_role,
        min_duration_days=payload.min_duration_days,
        is_default=payload.is_default,
    )
    session.add(workflow)
    await session.flush()

    for step in payload.steps:
        session.add(
            ApprovalWorkflowStep(
                tenant_id=actor.tenant_id,
                workflow_id=workflow.id,
                level=step.level,
                approver_type=step.approver_type,
                approver_user_id=step.approver_user_id,
                approver_role=step.approver_role,
                auto_approve_after_hours=step.auto_approve_after_hours,
                is_mandatory=step.is_mandatory,
            )
        )
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.CREATE,
        entity_type="approval_workflow",
        entity_id=workflow.id,
        actor_id=actor.id,
        actor_email=actor.email,
        after={"name": workflow.name, "levels": len(payload.steps)},
        channel=context.channel,
        request_id=context.request_id,
    )
    await session.refresh(workflow)
    return WorkflowRead.model_validate(workflow)


@router.delete(
    "/{workflow_id}",
    response_model=WorkflowRead,
    summary="Deactivate a workflow (HR)",
)
async def deactivate_workflow(
    workflow_id: uuid.UUID,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
) -> WorkflowRead:
    """
    Soft-delete only.

    In-flight requests reference this workflow; hard-deleting it would strand
    them mid-chain.
    """
    workflow = await session.get(ApprovalWorkflow, workflow_id)
    if workflow is None or workflow.tenant_id != actor.tenant_id:
        raise NotFoundError("Workflow not found.")
    workflow.is_active = False
    return WorkflowRead.model_validate(workflow)
