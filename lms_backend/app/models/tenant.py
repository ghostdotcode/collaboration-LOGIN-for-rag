"""Tenant (customer company) — the root of every ownership chain."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TenantStatus
from app.models.sa_types import sa_enum


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    status: Mapped[TenantStatus] = mapped_column(
        sa_enum(TenantStatus, "tenant_status"),
        nullable=False,
        default=TenantStatus.TRIAL,
    )

    # ── Subscription (Stripe) ────────────────────────────────────────────
    subscription_tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="starter"
    )
    employee_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String(64))
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Leave-year configuration ─────────────────────────────────────────
    # 1 = calendar year, 4 = Apr-Mar (common in IN/UK), etc.
    fiscal_year_start_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    default_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )

    sso_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def can_transact(self) -> bool:
        """Whether users of this tenant may perform write operations."""
        return self.status in {TenantStatus.TRIAL, TenantStatus.ACTIVE}
