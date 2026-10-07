"""SaaS billing schemas (admin surface)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import Field, HttpUrl, field_validator

from app.core.config import settings
from app.schemas.common import ORMSchema, SafeCode, StrictSchema


class SubscriptionRead(ORMSchema):
    """What the billing dashboard renders."""

    tenant_id: uuid.UUID
    name: str
    status: str
    subscription_tier: str
    employee_limit: int
    active_employees: int
    seats_remaining: int
    seats_used_percent: float
    stripe_customer_id: Optional[str]
    stripe_subscription_id: Optional[str]
    trial_ends_at: Optional[datetime]
    billing_enabled: bool


class _RedirectUrls(StrictSchema):
    """
    Redirect targets are attacker-controlled input.

    An open redirect here would let a phisher bounce a signed-in admin off our
    domain, so both URLs must sit under the configured app origin.
    """

    @staticmethod
    def _same_origin(value: HttpUrl) -> HttpUrl:
        # The admin UI may be served from the chatbot origin rather than the
        # API's own, so both are acceptable — anything else is not.
        allowed = [settings.APP_BASE_URL.rstrip("/")] + [
            origin.rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS
        ]
        if not any(str(value).startswith(origin) for origin in allowed):
            raise ValueError(f"URL must start with one of: {', '.join(allowed)}")
        return value


class CheckoutRequest(_RedirectUrls):
    price_id: str = Field(
        min_length=1, max_length=120, pattern=r"^price_[A-Za-z0-9_]+$"
    )
    success_url: HttpUrl
    cancel_url: HttpUrl

    @field_validator("success_url", "cancel_url")
    @classmethod
    def _validate_origin(cls, v: HttpUrl) -> HttpUrl:
        return cls._same_origin(v)


class PortalRequest(_RedirectUrls):
    return_url: HttpUrl

    @field_validator("return_url")
    @classmethod
    def _validate_origin(cls, v: HttpUrl) -> HttpUrl:
        return cls._same_origin(v)


class HostedSessionResponse(ORMSchema):
    url: str


class PlanRead(ORMSchema):
    tier: SafeCode
    seats: int
    is_current: bool


class WebhookAck(ORMSchema):
    received: bool = True
    outcome: str
