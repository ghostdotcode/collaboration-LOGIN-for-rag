"""
SaaS billing endpoints.

Everything except the webhook is admin-only. The webhook is deliberately
unauthenticated — it is called by Stripe, not by a user — and is protected
instead by HMAC signature verification over the raw request body.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_current_tenant, require_admin
from app.api.dependencies.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.billing import (
    CheckoutRequest,
    HostedSessionResponse,
    PlanRead,
    PortalRequest,
    SubscriptionRead,
    WebhookAck,
)
from app.services import billing_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])


@router.get(
    "/subscription",
    response_model=SubscriptionRead,
    summary="Current plan and seat usage (admin)",
)
async def subscription(
    _: User = Depends(require_admin),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> SubscriptionRead:
    used, remaining = await billing_service.seat_usage(session, tenant)
    limit = tenant.employee_limit or 1
    return SubscriptionRead(
        tenant_id=tenant.id,
        name=tenant.name,
        status=tenant.status.value,
        subscription_tier=tenant.subscription_tier,
        employee_limit=tenant.employee_limit,
        active_employees=used,
        seats_remaining=remaining,
        seats_used_percent=round(used / limit * 100, 1),
        stripe_customer_id=tenant.stripe_customer_id,
        stripe_subscription_id=tenant.stripe_subscription_id,
        trial_ends_at=tenant.trial_ends_at,
        billing_enabled=billing_service.is_enabled(),
    )


@router.get("/plans", response_model=List[PlanRead], summary="Available plans")
async def plans(
    _: User = Depends(require_admin),
    tenant: Tenant = Depends(get_current_tenant),
) -> List[PlanRead]:
    return [
        PlanRead(tier=tier, seats=seats, is_current=tier == tenant.subscription_tier)
        for tier, seats in billing_service.TIER_SEATS.items()
    ]


@router.post(
    "/checkout",
    response_model=HostedSessionResponse,
    summary="Start a Stripe Checkout session (admin)",
)
async def checkout(
    payload: CheckoutRequest,
    actor: User = Depends(require_admin),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
) -> HostedSessionResponse:
    url = await billing_service.create_checkout_session(
        session,
        tenant=tenant,
        actor=actor,
        price_id=payload.price_id,
        success_url=str(payload.success_url),
        cancel_url=str(payload.cancel_url),
    )
    return HostedSessionResponse(url=url)


@router.post(
    "/portal",
    response_model=HostedSessionResponse,
    summary="Open the Stripe billing portal (admin)",
)
async def portal(
    payload: PortalRequest,
    _: User = Depends(require_admin),
    tenant: Tenant = Depends(get_current_tenant),
) -> HostedSessionResponse:
    return HostedSessionResponse(
        url=billing_service.create_portal_session(
            tenant=tenant, return_url=str(payload.return_url)
        )
    )


@router.post(
    "/webhook",
    response_model=WebhookAck,
    summary="Stripe webhook receiver",
    include_in_schema=False,
)
async def webhook(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> WebhookAck:
    """
    Apply subscription lifecycle events.

    The body is read raw: Stripe signs the exact bytes it sent, so parsing
    first and re-serialising would break verification.
    """
    raw = await request.body()
    event = billing_service.verify_webhook(raw, request.headers.get("Stripe-Signature"))
    outcome = await billing_service.handle_event(session, event)
    logger.info("Stripe webhook %s -> %s", event.get("id"), outcome)
    return WebhookAck(received=True, outcome=outcome)
