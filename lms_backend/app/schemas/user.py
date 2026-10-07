"""Employee/user schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import EmailStr, Field, model_validator

from app.models.enums import UserRole
from app.schemas.common import (
    ORMSchema,
    Password,
    SafeCode,
    SafeLine,
    StrictSchema,
    TimezoneName,
)


class UserBase(StrictSchema):
    email: EmailStr
    first_name: SafeLine = Field(max_length=80)
    last_name: SafeLine = Field(max_length=80)
    role: UserRole = UserRole.EMPLOYEE
    manager_id: Optional[uuid.UUID] = None
    location_id: Optional[uuid.UUID] = None
    employee_code: Optional[SafeCode] = None
    timezone: Optional[TimezoneName] = None
    date_of_joining: date


class UserCreate(UserBase):
    # Omitted for SSO-only provisioning (user signs in via the chatbot).
    password: Optional[Password] = None


class UserUpdate(StrictSchema):
    first_name: Optional[SafeLine] = Field(default=None, max_length=80)
    last_name: Optional[SafeLine] = Field(default=None, max_length=80)
    role: Optional[UserRole] = None
    manager_id: Optional[uuid.UUID] = None
    location_id: Optional[uuid.UUID] = None
    timezone: Optional[TimezoneName] = None
    date_of_exit: Optional[date] = None
    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class UserBrief(ORMSchema):
    """Minimal projection for team widgets and approver lists."""

    id: uuid.UUID
    first_name: str
    last_name: str
    email: EmailStr
    role: UserRole


class UserRead(ORMSchema):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    first_name: str
    last_name: str
    role: UserRole
    manager_id: Optional[uuid.UUID]
    location_id: Optional[uuid.UUID]
    employee_code: Optional[str]
    timezone: Optional[str]
    date_of_joining: date
    date_of_exit: Optional[date]
    is_active: bool
    last_login_at: Optional[datetime]
    created_at: datetime


class TeamMemberAway(ORMSchema):
    """Row of the 'who is away' widget."""

    user_id: uuid.UUID
    full_name: str
    leave_type: str
    start_date: date
    end_date: date
