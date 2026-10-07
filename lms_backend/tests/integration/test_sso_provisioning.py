"""
SSO hand-off from the chatbot, including just-in-time provisioning.

A person who signed up on the chatbot has no LMS profile. Previously the
"Apply for Leave" button dead-ended with "ask HR to add you". With
SSO_AUTO_PROVISION on, the profile is created - as an EMPLOYEE only, within the
seat limit, optionally restricted to allowed email domains, and audited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import func, select

from app.api.dependencies.database import get_db
from app.core.config import settings
from app.main import app
from app.models.audit_log import AuditLog
from app.models.enums import UserRole
from app.models.user import User

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE = "http://testserver/api/v1"


def chatbot_token(email, **claims):
    payload = {"sub": email, "exp": datetime.now(timezone.utc) + timedelta(hours=1), **claims}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


@pytest.fixture
def http(session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", False)
    monkeypatch.setattr(settings, "SSO_ALLOWED_EMAIL_DOMAINS", [])
    app.dependency_overrides[get_db] = lambda: session
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    yield client
    app.dependency_overrides.clear()


async def exchange(http, seeded, token):
    return await http.post(
        "/auth/sso/exchange", json={"chatbot_token": token, "tenant_slug": seeded["tenant"].slug}
    )


async def count_users(session, seeded):
    return await session.scalar(select(func.count(User.id)).where(User.tenant_id == seeded["tenant"].id))


async def test_existing_employee_signs_in(http, seeded):
    async with http:
        r = await exchange(http, seeded, chatbot_token("employee@acme.example"))
    assert r.status_code == 200 and r.json()["role"] == "employee"


async def test_mixed_case_token_subject_still_matches(http, seeded):
    async with http:
        r = await exchange(http, seeded, chatbot_token("Employee@ACME.example"))
    assert r.status_code == 200 and r.json()["user_id"] == str(seeded["employee"].id)


async def test_unknown_user_is_refused_when_provisioning_is_off(http, session, seeded):
    before = await count_users(session, seeded)
    async with http:
        r = await exchange(http, seeded, chatbot_token("newhire@acme.example"))
    assert r.status_code == 401
    assert await count_users(session, seeded) == before


async def test_unknown_user_is_provisioned_when_enabled(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    async with http:
        r = await exchange(
            http, seeded, chatbot_token("newhire@acme.example", first_name="Nia", last_name="Njeri")
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "employee" and body["full_name"] == "Nia Njeri"

    user = await session.get(User, __import__("uuid").UUID(body["user_id"]))
    assert user.password_hash is None and user.is_active and user.tenant_id == seeded["tenant"].id
    await session.flush()
    rows = (await session.scalars(
        select(AuditLog).where(AuditLog.entity_id == user.id, AuditLog.entity_type == "user")
    )).all()
    assert any((a.after_state or {}).get("source") == "sso_auto_provision" for a in rows)


async def test_second_exchange_does_not_duplicate(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    async with http:
        a = await exchange(http, seeded, chatbot_token("twice@acme.example"))
        b = await exchange(http, seeded, chatbot_token("TWICE@acme.example"))
    assert a.json()["user_id"] == b.json()["user_id"]


async def test_name_falls_back_to_full_name_then_email(http, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    async with http:
        a = await exchange(http, seeded, chatbot_token("a.b@acme.example", name="Zed Quartz Jr"))
        b = await exchange(http, seeded, chatbot_token("jane.doe@acme.example"))
    assert a.json()["full_name"] == "Zed Quartz Jr"
    assert b.json()["full_name"].startswith("Jane Doe")


async def test_token_claims_can_never_grant_a_role(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    async with http:
        r = await exchange(http, seeded, chatbot_token("sneaky@acme.example", role="hr_admin", is_admin=True))
    assert r.json()["role"] == "employee"


async def test_domain_allow_list(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    monkeypatch.setattr(settings, "SSO_ALLOWED_EMAIL_DOMAINS", ["acme.example"])
    before = await count_users(session, seeded)
    async with http:
        bad = await exchange(http, seeded, chatbot_token("outsider@gmail.com"))
        good = await exchange(http, seeded, chatbot_token("insider@acme.example"))
    assert bad.status_code == 401 and good.status_code == 200
    assert await count_users(session, seeded) == before + 1


async def test_seat_limit_blocks_provisioning(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    seeded["tenant"].employee_limit = await count_users(session, seeded)
    await session.flush()
    async with http:
        r = await exchange(http, seeded, chatbot_token("overflow@acme.example"))
    assert r.status_code == 402
    assert "seat_limit_reached" in r.text


async def test_deactivated_user_cannot_be_resurrected_by_sso(http, session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    seeded["employee"].is_active = False
    await session.flush()
    before = await count_users(session, seeded)
    async with http:
        r = await exchange(http, seeded, chatbot_token("employee@acme.example"))
    assert r.status_code == 401 and await count_users(session, seeded) == before


@pytest.mark.parametrize("sub", ["", "no-at-sign", "   "])
async def test_malformed_subjects_rejected(http, seeded, monkeypatch, sub):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    async with http:
        r = await exchange(http, seeded, chatbot_token(sub or " "))
    assert r.status_code == 401


async def test_expired_and_forged_tokens_rejected(http, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    old = jwt.encode({"sub": "x@acme.example", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                     settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    forged = jwt.encode({"sub": "x@acme.example", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "attacker-secret-attacker-secret-1234", algorithm="HS256")
    async with http:
        assert (await exchange(http, seeded, old)).status_code == 401
        assert (await exchange(http, seeded, forged)).status_code == 401
