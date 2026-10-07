"""
Model package. Importing this module registers every mapper, which is what
Alembic autogenerate and `Base.metadata.create_all` rely on.
"""

from app.db.base_class import Base
from app.models.approval_workflow import (
    ApprovalWorkflow,
    ApprovalWorkflowStep,
    LeaveApproval,
)
from app.models.audit_log import AuditLog
from app.models.delegation import Delegation
from app.models.enums import (
    AccrualFrequency,
    ActionChannel,
    ApprovalStatus,
    ApproverType,
    AuditAction,
    DayPart,
    LeaveStatus,
    LeaveUnit,
    TenantStatus,
    UserRole,
)
from app.models.holiday_calendar import Holiday, HolidayCalendar
from app.models.leave_balance import LeaveBalance
from app.models.leave_policy import LeavePolicy
from app.models.leave_request import LeaveRequest
from app.models.leave_type import LeaveType
from app.models.location import Location
from app.models.tenant import Tenant
from app.models.user import User

# Grouped by kind rather than alphabetically: the entity list doubles as a
# map of the domain.
__all__ = [  # noqa: RUF022
    "Base",
    # entities
    "Tenant",
    "Location",
    "User",
    "LeaveType",
    "LeavePolicy",
    "LeaveBalance",
    "LeaveRequest",
    "ApprovalWorkflow",
    "ApprovalWorkflowStep",
    "LeaveApproval",
    "Delegation",
    "HolidayCalendar",
    "Holiday",
    "AuditLog",
    # enums
    "AccrualFrequency",
    "ActionChannel",
    "ApprovalStatus",
    "ApproverType",
    "AuditAction",
    "DayPart",
    "LeaveStatus",
    "LeaveUnit",
    "TenantStatus",
    "UserRole",
]

# Tables that carry tenant_id and therefore need an RLS policy. The
# migration and a guard test both read this list, so adding a tenant-scoped
# model without protecting it fails CI.
TENANT_SCOPED_TABLES = [
    mapper.class_.__tablename__
    for mapper in Base.registry.mappers
    if "tenant_id" in mapper.class_.__table__.columns
]
