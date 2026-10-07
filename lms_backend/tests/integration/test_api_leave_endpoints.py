"""
HTTP-layer tests for the leave endpoints.

The service-level tests exercise the business rules; these exist because a
route can still fail *after* the service succeeds. The regression that
prompted them: submitting a leave request returned 500 because serialising
the freshly inserted row into `LeaveRequestRead` lazy-loaded `created_at`
and the approval chain, which the async engine cannot do mid-response.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.api.dependencies.auth import get_current_tenant, get_current_user
from app.api.dependencies.database import get_db
from app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE = "http://testserver/api/v1"


@pytest.fixture
def client(session, seeded):
    """
    An ASGI client wired to the test session and signed in as the employee.

    Auth and the DB session are overridden rather than faked at the HTTP
    boundary so the routes, response models and dependency plumbing all run
    exactly as they do in production.
    """
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: seeded["employee"]
    app.dependency_overrides[get_current_tenant] = lambda: seeded["tenant"]
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url=BASE)
    app.dependency_overrides.clear()


def _body(leave_type_id, start, end=None, **extra):
    return {
        "leave_type_id": str(leave_type_id),
        "start_date": start.isoformat(),
        "end_date": (end or start).isoformat(),
        "start_day_part": "full_day",
        "end_day_part": "full_day",
        **extra,
    }


async def test_submit_returns_the_serialised_request(
    client, session, seeded, next_monday
):
    async with client as http:
        response = await http.post(
            "/leaves",
            json=_body(seeded["leave_type"].id, next_monday, reason="Short break"),
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["duration_days"] == "1.00"
    # The fields that previously triggered a lazy load mid-serialisation.
    assert body["created_at"] is not None
    assert body["user"]["id"] == str(seeded["employee"].id)
    assert len(body["approvals"]) >= 1


async def test_estimate_does_not_create_anything(client, seeded, next_monday):
    async with client as http:
        response = await http.post(
            "/leaves/estimate",
            json=_body(seeded["leave_type"].id, next_monday),
        )
        assert response.status_code == 200, response.text
        assert response.json()["working_days"] == "1.00"

        listed = await http.get("/leaves")

    assert listed.status_code == 200
    assert listed.json()["total"] == 0


async def test_overlapping_submission_is_rejected_with_a_domain_error(
    client, seeded, next_monday
):
    """The second attempt must be a readable 409, not a 500."""
    async with client as http:
        first = await http.post("/leaves", json=_body(seeded["leave_type"].id, next_monday))
        assert first.status_code == 201, first.text

        second = await http.post(
            "/leaves", json=_body(seeded["leave_type"].id, next_monday)
        )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "overlapping_leave"


async def test_contradictory_dates_are_rejected_before_any_write(
    client, seeded, next_monday
):
    async with client as http:
        response = await http.post(
            "/leaves",
            json=_body(
                seeded["leave_type"].id, next_monday, next_monday - timedelta(days=2)
            ),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
