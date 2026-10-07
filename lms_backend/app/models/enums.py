"""Shared enumerations. Values are the wire format — never rename casually."""

from enum import Enum


class TenantStatus(str, Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class UserRole(str, Enum):
    ADMIN = "admin"
    HR = "hr"
    MANAGER = "manager"
    EMPLOYEE = "employee"


class LeaveUnit(str, Enum):
    DAY = "day"
    HOUR = "hour"


class DayPart(str, Enum):
    """Which portion of the boundary day the leave covers."""

    FULL_DAY = "full_day"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"


class LeaveStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {LeaveStatus.REJECTED, LeaveStatus.CANCELLED}


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class ApproverType(str, Enum):
    """How an approval step resolves to a concrete person."""

    REPORTING_MANAGER = "reporting_manager"
    SKIP_LEVEL_MANAGER = "skip_level_manager"
    HR = "hr"
    SPECIFIC_USER = "specific_user"
    ROLE = "role"


class ActionChannel(str, Enum):
    WEB = "web"
    EMAIL = "email"
    API = "api"
    SYSTEM = "system"


class AccrualFrequency(str, Enum):
    NONE = "none"  # fixed annual quota, no periodic accrual
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUALLY = "annually"


class AuditAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"
    LOGIN = "login"
    LOGOUT = "logout"
    POLICY_VERSION = "policy_version"
    ACCRUAL = "accrual"
    ROLLOVER = "rollover"
    DELEGATE = "delegate"
