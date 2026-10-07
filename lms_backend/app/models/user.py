"""Employee record, including the self-referential reporting hierarchy."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    qualified,
)
from app.models.enums import UserRole
from app.models.sa_types import sa_enum


class User(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"
    # Email is unique *per tenant*, not globally: the same consultant may
    # legitimately exist in two customer workspaces.
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        UniqueConstraint("tenant_id", "employee_code"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Null for SSO-only users who never set a local password.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))

    role: Mapped[UserRole] = mapped_column(
        sa_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.EMPLOYEE,
        index=True,
    )

    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("users") + ".id", ondelete="SET NULL"),
        index=True,
    )
    location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(qualified("locations") + ".id", ondelete="SET NULL"),
        index=True,
    )

    employee_code: Mapped[Optional[str]] = mapped_column(String(32))
    # Per-user override; falls back to the location's timezone when null.
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    date_of_joining: Mapped[date] = mapped_column(Date, nullable=False)
    date_of_exit: Mapped[Optional[date]] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Relationships ────────────────────────────────────────────────────
    location: Mapped[Optional["Location"]] = relationship(lazy="selectin")
    manager: Mapped[Optional["User"]] = relationship(
        "User", remote_side="User.id", back_populates="direct_reports"
    )
    direct_reports: Mapped[List["User"]] = relationship(
        "User", back_populates="manager"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def effective_timezone(self) -> str:
        """User override > office timezone > UTC."""
        if self.timezone:
            return self.timezone
        location = self.__dict__.get("location")
        if location is not None and location.timezone:
            return location.timezone
        return "UTC"

    @property
    def is_people_manager(self) -> bool:
        return self.role in {UserRole.MANAGER, UserRole.HR, UserRole.ADMIN}


from app.models.location import Location  # noqa: E402  (resolve relationship target)
