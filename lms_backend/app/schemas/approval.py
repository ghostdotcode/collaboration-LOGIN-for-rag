"""Approval, bulk-approval, workflow template and delegation schemas."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, model_validator

from app.models.enums import ApproverType, UserRole
from app.schemas.common import ORMSchema, SafeLine, SafeText, StrictSchema


class ApprovalDecisionRequest(StrictSchema):
    comment: Optional[SafeText] = Field(default=None, max_length=1000)


class RejectionRequest(StrictSchema):
    # A rejection without a reason generates a support ticket every time.
    comment: SafeText = Field(min_length=3, max_length=1000)


class BulkApprovalRequest(StrictSchema):
    request_ids: List[uuid.UUID] = Field(min_length=1, max_length=100)
    decision: str = Field(pattern=r"^(approve|reject)$")
    comment: Optional[SafeText] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reject_needs_comment(self) -> "BulkApprovalRequest":
        if self.decision == "reject" and not self.comment:
            raise ValueError("A comment is required when rejecting requests")
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("request_ids contains duplicates")
        return self


class BulkApprovalResultRow(ORMSchema):
    request_id: uuid.UUID
    succeeded: bool
    error_code: Optional[str] = None
    message: Optional[str] = None


class BulkApprovalResponse(ORMSchema):
    processed: int
    succeeded: int
    failed: int
    results: List[BulkApprovalResultRow]


# ── Workflow templates ─────────────────────────────────────────────────────


class WorkflowStepCreate(StrictSchema):
    level: int = Field(ge=1, le=10)
    approver_type: ApproverType = ApproverType.REPORTING_MANAGER
    approver_user_id: Optional[uuid.UUID] = None
    approver_role: Optional[UserRole] = None
    auto_approve_after_hours: Optional[int] = Field(default=None, ge=1, le=720)
    is_mandatory: bool = True

    @model_validator(mode="after")
    def _validate_target(self) -> "WorkflowStepCreate":
        if (
            self.approver_type == ApproverType.SPECIFIC_USER
            and not self.approver_user_id
        ):
            raise ValueError(
                "approver_user_id is required for approver_type 'specific_user'"
            )
        if self.approver_type == ApproverType.ROLE and not self.approver_role:
            raise ValueError("approver_role is required for approver_type 'role'")
        return self


class WorkflowCreate(StrictSchema):
    name: SafeLine
    leave_type_id: Optional[uuid.UUID] = None
    applies_to_location_id: Optional[uuid.UUID] = None
    applies_to_role: Optional[UserRole] = None
    min_duration_days: Decimal = Field(default=Decimal("0"), ge=0, le=365)
    is_default: bool = False
    steps: List[WorkflowStepCreate] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _validate_levels(self) -> "WorkflowCreate":
        levels = [step.level for step in self.steps]
        if sorted(levels) != list(range(1, len(levels) + 1)):
            raise ValueError("Step levels must be consecutive and start at 1")
        return self


class WorkflowStepRead(ORMSchema):
    id: uuid.UUID
    level: int
    approver_type: ApproverType
    approver_user_id: Optional[uuid.UUID]
    approver_role: Optional[UserRole]
    auto_approve_after_hours: Optional[int]
    is_mandatory: bool


class WorkflowRead(ORMSchema):
    id: uuid.UUID
    name: str
    leave_type_id: Optional[uuid.UUID]
    applies_to_location_id: Optional[uuid.UUID]
    applies_to_role: Optional[UserRole]
    min_duration_days: Decimal
    is_default: bool
    is_active: bool
    steps: List[WorkflowStepRead] = Field(default_factory=list)


# ── Delegation ─────────────────────────────────────────────────────────────


class DelegationCreate(StrictSchema):
    delegate_id: uuid.UUID
    starts_on: date
    ends_on: date
    reason: Optional[SafeText] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate_window(self) -> "DelegationCreate":
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on cannot be before starts_on")
        return self


class DelegationRead(ORMSchema):
    id: uuid.UUID
    delegator_id: uuid.UUID
    delegate_id: uuid.UUID
    starts_on: date
    ends_on: date
    reason: Optional[str]
    is_active: bool
