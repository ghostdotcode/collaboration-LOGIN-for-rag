"""
Unit tests for the billing webhook boundary.

Signature verification is the only part of billing an unauthenticated caller
can reach, so it is tested exhaustively: forgery, replay, malformed headers
and key rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.core.config import settings
from app.services import billing_service
from app.services.billing_service import (
    BillingNotConfiguredError,
    WebhookVerificationError,
    seats_for_tier,
    verify_webhook,
)

SECRET = "whsec_test_secret"


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", SECRET)
    return SECRET


def sign(payload: bytes, *, secret: str = SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={digest}"


def body(**overrides) -> bytes:
    event = {"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}
    event.update(overrides)
    return json.dumps(event).encode()


def test_a_correctly_signed_event_is_accepted():
    payload = body()
    event = verify_webhook(payload, sign(payload))
    assert event["id"] == "evt_1"


def test_a_tampered_body_is_rejected():
    payload = body()
    signature = sign(payload)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body(type="customer.subscription.deleted"), signature)


def test_a_signature_from_the_wrong_secret_is_rejected():
    payload = body()
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, sign(payload, secret="whsec_attacker"))


def test_an_old_signature_is_rejected_as_a_replay():
    payload = body()
    stale = int(time.time()) - billing_service.WEBHOOK_TOLERANCE_SECONDS - 5
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, sign(payload, timestamp=stale))


def test_a_signature_inside_the_tolerance_window_is_accepted():
    payload = body()
    recent = int(time.time()) - billing_service.WEBHOOK_TOLERANCE_SECONDS + 30
    assert verify_webhook(payload, sign(payload, timestamp=recent))["id"] == "evt_1"


def test_rotated_keys_are_accepted_when_either_signature_matches():
    """During a secret rotation Stripe sends one v1 entry per active key."""
    payload = body()
    valid = sign(payload).split("v1=")[1]
    header = f"t={int(time.time())},v1=deadbeef,v1={valid}"
    assert verify_webhook(payload, header)["id"] == "evt_1"


@pytest.mark.parametrize(
    "header",
    [None, "", "garbage", "t=123", "v1=abc", "t=notanumber,v1=abc"],
)
def test_malformed_headers_are_rejected(header):
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body(), header)


def test_a_non_json_body_is_rejected():
    payload = b"\x00not json"
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, sign(payload))


def test_verification_fails_closed_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", None)
    payload = body()
    with pytest.raises(BillingNotConfiguredError):
        verify_webhook(payload, sign(payload))


def test_unknown_tiers_fall_back_to_the_smallest_plan():
    assert seats_for_tier("growth") == billing_service.TIER_SEATS["growth"]
    assert seats_for_tier("made_up") == billing_service.TIER_SEATS["starter"]


@pytest.mark.parametrize(
    "price, expected",
    [
        ({"lookup_key": "enterprise"}, "enterprise"),
        ({"lookup_key": None, "nickname": "Growth"}, "growth"),
        ({"lookup_key": "legacy_plan"}, "starter"),
        ({}, "starter"),
    ],
)
def test_tier_is_read_from_the_price_lookup_key(price, expected):
    subscription = {"items": {"data": [{"price": price}]}}
    assert billing_service._tier_from_subscription(subscription) == expected


def test_tier_defaults_when_the_subscription_has_no_items():
    assert billing_service._tier_from_subscription({}) == "starter"
