"""
SaaS subscription billing (Stripe).

Two deliberate design choices:

* The Stripe SDK is imported lazily. Billing is optional — a self-hosted or
  air-gapped deployment must still boot, and the API pods should not fail
  readiness because a payments vendor is unreachable.
* Webhook signatures are verified locally with `hmac`, not via the SDK. It is
  twenty lines, it is the part an attacker attacks, and it stays unit-testable
  without network access or a vendor stub.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    LMSError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.models.enums import ActionChannel, AuditAction, TenantStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service

logger = logging.getLogger(__name__)

# Seat entitlement per plan. The price IDs are configured in Stripe and mapped
# here by lookup key so a price change in the dashboard does not need a deploy.
TIER_SEATS: Dict[str, int] = {
    "starter": 50,
    "growth": 250,
    "enterprise": 2000,
}
DEFAULT_TIER = "starter"

# Stripe rejects timestamps older than this to blunt replay attacks; we apply
# the same window ourselves.
WEBHOOK_TOLERANCE_SECONDS = 300


class BillingNotConfiguredError(LMSError):
    status_code = 501
    code = "billing_not_configured"
    message = "Billing is not enabled for this deployment."


class WebhookVerificationError(LMSError):
    status_code = 400
    code = "invalid_webhook_signature"
    message = "The webhook signature could not be verified."


def is_enabled() -> bool:
    return bool(settings.STRIPE_API_KEY)


def _client():
    """Return a configured Stripe module, or raise a domain error."""
    if not is_enabled():
        raise BillingNotConfiguredError()
    try:
        import stripe
    except ImportError as exc:  # pragma: no cover - deployment misconfiguration
        raise ServiceUnavailableError(
            "The Stripe SDK is not installed in this image."
        ) from exc
    stripe.api_key = settings.STRIPE_API_KEY
    stripe.max_network_retries = 2
    return stripe


def seats_for_tier(tier: str) -> int:
    return TIER_SEATS.get(tier, TIER_SEATS[DEFAULT_TIER])


async def seat_usage(session: AsyncSession, tenant: Tenant) -> Tuple[int, int]:
    """(active employees, seats remaining) — the number we bill on."""
    active = await session.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == tenant.id, User.is_active.is_(True)
        )
    )
    used = int(active or 0)
    return used, max(tenant.employee_limit - used, 0)


async def create_checkout_session(
    session: AsyncSession,
    *,
    tenant: Tenant,
    actor: User,
    price_id: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """
    Start a Stripe Checkout flow for a plan change.

    Quantity is the *current* active headcount: the tenant pays for the seats
    they actually use, and Stripe prorates on the next invoice.
    """
    stripe = _client()
    used, _ = await seat_usage(session, tenant)

    try:
        checkout = stripe.checkout.Session.create(
            mode="subscription",
            customer=tenant.stripe_customer_id or None,
            customer_email=None if tenant.stripe_customer_id else actor.email,
            client_reference_id=str(tenant.id),
            line_items=[{"price": price_id, "quantity": max(used, 1)}],
            success_url=success_url,
            cancel_url=cancel_url,
            # Lets the webhook attribute the subscription without trusting
            # anything the browser sends back on the success redirect.
            subscription_data={"metadata": {"tenant_id": str(tenant.id)}},
            metadata={"tenant_id": str(tenant.id)},
        )
    except Exception as exc:
        logger.exception("Stripe checkout creation failed")
        raise ServiceUnavailableError("Could not reach the payment provider.") from exc

    await audit_service.record(
        session,
        tenant_id=tenant.id,
        actor_id=actor.id,
        actor_email=actor.email,
        action=AuditAction.UPDATE,
        entity_type="billing",
        entity_id=tenant.id,
        after={"checkout_session": checkout.id, "price_id": price_id},
    )
    return checkout.url


def create_portal_session(*, tenant: Tenant, return_url: str) -> str:
    """Hand the customer to Stripe's hosted billing portal."""
    stripe = _client()
    if not tenant.stripe_customer_id:
        raise ValidationFailedError(
            "This workspace has no subscription yet. Start a plan first."
        )
    try:
        portal = stripe.billing_portal.Session.create(
            customer=tenant.stripe_customer_id, return_url=return_url
        )
    except Exception as exc:
        logger.exception("Stripe portal creation failed")
        raise ServiceUnavailableError("Could not reach the payment provider.") from exc
    return portal.url


# ── Webhooks ──────────────────────────────────────────────────────────────


def verify_webhook(payload: bytes, signature_header: Optional[str]) -> Dict[str, Any]:
    """
    Validate a `Stripe-Signature` header and return the parsed event.

    Header format: ``t=<unix ts>,v1=<hex hmac>[,v1=<rotated key hmac>]``.
    The signed message is ``"{t}.{raw body}"`` — the *raw* body, which is why
    the route must read `await request.body()` and not a parsed model.
    """
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise BillingNotConfiguredError("No webhook secret is configured.")
    if not signature_header:
        raise WebhookVerificationError("Missing Stripe-Signature header.")

    timestamp: Optional[str] = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        raise WebhookVerificationError("Malformed Stripe-Signature header.")

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise WebhookVerificationError("Malformed signature timestamp.") from exc
    if age > WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookVerificationError("Webhook timestamp outside tolerance.")

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + payload,
        hashlib.sha256,
    ).hexdigest()

    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookVerificationError()

    import json

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookVerificationError("Webhook body was not valid JSON.") from exc


async def _tenant_for_event(
    session: AsyncSession, obj: Dict[str, Any]
) -> Optional[Tenant]:
    """
    Resolve the tenant an event belongs to.

    Prefers our own metadata over Stripe's customer id, because the metadata
    is written by us at checkout and survives customer merges.
    """
    metadata = obj.get("metadata") or {}
    raw_id = metadata.get("tenant_id")
    if raw_id:
        try:
            return await session.get(Tenant, uuid.UUID(raw_id))
        except (ValueError, TypeError):
            logger.warning("Webhook carried an unparseable tenant_id: %r", raw_id)

    customer_id = obj.get("customer")
    if isinstance(customer_id, str):
        return await session.scalar(
            select(Tenant).where(Tenant.stripe_customer_id == customer_id)
        )
    return None


def _tier_from_subscription(subscription: Dict[str, Any]) -> str:
    """Read the plan's lookup key; fall back to the nickname, then starter."""
    items = (subscription.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        for key in ("lookup_key", "nickname"):
            value = price.get(key)
            if isinstance(value, str) and value.lower() in TIER_SEATS:
                return value.lower()
    return DEFAULT_TIER


async def handle_event(session: AsyncSession, event: Dict[str, Any]) -> str:
    """
    Apply a verified Stripe event.

    Returns a short human-readable outcome for the response body and the logs.
    Unknown event types are acknowledged, not rejected: Stripe retries 4xx/5xx
    responses for days, and an unrecognised type is not a failure.
    """
    event_type = event.get("type", "")
    obj = ((event.get("data") or {}).get("object")) or {}

    handled = {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
        "invoice.paid",
    }
    if event_type not in handled:
        return f"ignored:{event_type}"

    tenant = await _tenant_for_event(session, obj)
    if tenant is None:
        # Do not 404: that would make Stripe retry forever for an event about
        # a workspace we legitimately do not have (e.g. a deleted trial).
        logger.warning("Stripe event %s matched no tenant", event.get("id"))
        return "no_tenant"

    # Webhooks arrive unauthenticated, so no dependency has pinned the session
    # to a tenant yet. `audit_logs` is RLS-protected with FORCE, and the insert
    # below would be rejected without this.
    from app.db.session import set_tenant_context

    await set_tenant_context(session, tenant.id)

    before = {
        "status": tenant.status,
        "subscription_tier": tenant.subscription_tier,
        "employee_limit": tenant.employee_limit,
    }

    if event_type == "checkout.session.completed":
        tenant.stripe_customer_id = obj.get("customer") or tenant.stripe_customer_id
        tenant.stripe_subscription_id = (
            obj.get("subscription") or tenant.stripe_subscription_id
        )
        tenant.status = TenantStatus.ACTIVE

    elif event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
    }:
        tenant.stripe_subscription_id = obj.get("id") or tenant.stripe_subscription_id
        tier = _tier_from_subscription(obj)
        tenant.subscription_tier = tier
        tenant.employee_limit = seats_for_tier(tier)
        stripe_status = obj.get("status")
        if stripe_status in {"active", "trialing"}:
            tenant.status = (
                TenantStatus.TRIAL
                if stripe_status == "trialing"
                else TenantStatus.ACTIVE
            )
        elif stripe_status in {"past_due", "unpaid"}:
            tenant.status = TenantStatus.PAST_DUE
        elif stripe_status in {"canceled", "incomplete_expired"}:
            tenant.status = TenantStatus.CANCELLED

    elif event_type == "customer.subscription.deleted":
        # Suspended, not deleted: HR data outlives the subscription, and a
        # win-back must not require re-onboarding 500 employees.
        tenant.status = TenantStatus.SUSPENDED
        tenant.stripe_subscription_id = None

    elif event_type == "invoice.payment_failed":
        tenant.status = TenantStatus.PAST_DUE

    elif event_type == "invoice.paid":
        if tenant.status == TenantStatus.PAST_DUE:
            tenant.status = TenantStatus.ACTIVE

    await audit_service.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="tenant_subscription",
        entity_id=tenant.id,
        actor_email="stripe-webhook",
        channel=ActionChannel.SYSTEM,
        before=before,
        after={
            "status": tenant.status,
            "subscription_tier": tenant.subscription_tier,
            "employee_limit": tenant.employee_limit,
            "stripe_event": event.get("id"),
            "stripe_event_type": event_type,
        },
    )
    return f"applied:{event_type}"
