"""
Database-backed tests for the leave lifecycle.

The headline case is `test_concurrent_submissions_only_one_succeeds`, which
reproduces the spec's double-booking scenario — the same employee submitting
from two tabs at the same millisecond — and asserts that the row lock lets
exactly one through.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.context import RequestContext
from app.core.exceptions import (
    InsufficientBalanceError,
    OverlappingLeaveError,
    PolicyViolationError,
)
from app.models.enums import DayPart, LeaveStatus
from app.models.leave_balance import LeaveBalance
from app.schemas.leave_request import LeaveRequestCreate
from app.services import approval_service, leave_request_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="pytest")


def _payload(leave_type_id, start, end=None, **overrides):
    return LeaveRequestCreate(
        leave_type_id=leave_type_id,
        start_date=start,
        end_date=end or start,
        **overrides,
    )


async def test_submit_reserves_pending_days(session, seeded, next_monday):
    request = await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(seeded["leave_type"].id, next_monday),
        context=CONTEXT,
    )
    await session.commit()

    assert request.status == LeaveStatus.PENDING
    assert request.duration_days == Decimal("1.00")

    balance = await session.get(LeaveBalance, seeded["balance"].id)
    await session.refresh(balance)
    # Reserved, not yet consumed: pending holds the claim until approval.
    assert balance.pending_days == Decimal("1.00")
    assert balance.used_days == Decimal("0.00")
    assert balance.available_days == Decimal("1.00")


async def test_half_day_costs_half(session, seeded, next_monday):
    request = await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(
            seeded["leave_type"].id,
            next_monday,
            start_day_part=DayPart.FIRST_HALF,
            end_day_part=DayPart.FIRST_HALF,
        ),
        context=CONTEXT,
    )
    await session.commit()
    assert request.duration_days == Decimal("0.50")


async def test_insufficient_balance_is_rejected(session, seeded, next_monday):
    # Balance holds 2 days; ask for a full week.
    with pytest.raises(InsufficientBalanceError):
        await leave_request_service.create_leave_request(
            session,
            user=seeded["employee"],
            tenant=seeded["tenant"],
            payload=_payload(
                seeded["leave_type"].id, next_monday, next_monday + timedelta(days=4)
            ),
            context=CONTEXT,
        )


async def test_overlapping_request_is_rejected(session, seeded, next_monday):
    await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(seeded["leave_type"].id, next_monday),
        context=CONTEXT,
    )
    await session.commit()

    with pytest.raises(OverlappingLeaveError):
        await leave_request_service.create_leave_request(
            session,
            user=seeded["employee"],
            tenant=seeded["tenant"],
            payload=_payload(seeded["leave_type"].id, next_monday),
            context=CONTEXT,
        )


async def test_retroactive_request_blocked_when_policy_forbids(session, seeded, today):
    with pytest.raises(PolicyViolationError):
        await leave_request_service.create_leave_request(
            session,
            user=seeded["employee"],
            tenant=seeded["tenant"],
            payload=_payload(seeded["leave_type"].id, today - timedelta(days=10)),
            context=CONTEXT,
        )


async def test_concurrent_submissions_only_one_succeeds(
    session_factory, seeded, next_monday
):
    """
    The double-booking race.

    One day of balance remains after the first reservation, so two
    simultaneous one-day requests (on different dates, to bypass the overlap
    check) must resolve to exactly one success and one rejection — never two
    approvals against the same day of entitlement.
    """
    # Leave exactly 1.0 day available.
    async with session_factory() as setup:
        balance = await setup.get(LeaveBalance, seeded["balance"].id)
        balance.opening_days = Decimal("1.00")
        await setup.commit()

    async def submit(offset: int):
        async with session_factory() as db:
            try:
                await leave_request_service.create_leave_request(
                    db,
                    user=await db.merge(seeded["employee"]),
                    tenant=await db.merge(seeded["tenant"]),
                    payload=_payload(
                        seeded["leave_type"].id, next_monday + timedelta(days=offset)
                    ),
                    context=CONTEXT,
                )
                await db.commit()
                return "ok"
            except InsufficientBalanceError:
                await db.rollback()
                return "rejected"

    results = await asyncio.gather(submit(0), submit(1))

    assert sorted(results) == ["ok", "rejected"], (
        f"expected exactly one success, got {results}"
    )

    async with session_factory() as verify:
        balance = await verify.get(LeaveBalance, seeded["balance"].id)
        assert balance.pending_days == Decimal("1.00")
        assert balance.available_days == Decimal("0.00")


async def test_approval_converts_pending_to_used(session, seeded, next_monday):
    request = await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(seeded["leave_type"].id, next_monday),
        context=CONTEXT,
    )
    await session.commit()

    await approval_service.decide(
        session,
        leave_request=request,
        actor=seeded["manager"],
        approve=True,
        comment="Enjoy",
        context=CONTEXT,
    )
    await session.commit()

    assert request.status == LeaveStatus.APPROVED
    balance = await session.get(LeaveBalance, seeded["balance"].id)
    await session.refresh(balance)
    assert balance.pending_days == Decimal("0.00")
    assert balance.used_days == Decimal("1.00")


async def test_rejection_releases_the_reservation(session, seeded, next_monday):
    request = await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(seeded["leave_type"].id, next_monday),
        context=CONTEXT,
    )
    await session.commit()

    await approval_service.decide(
        session,
        leave_request=request,
        actor=seeded["manager"],
        approve=False,
        comment="Release week, sorry",
        context=CONTEXT,
    )
    await session.commit()

    assert request.status == LeaveStatus.REJECTED
    balance = await session.get(LeaveBalance, seeded["balance"].id)
    await session.refresh(balance)
    assert balance.pending_days == Decimal("0.00")
    assert balance.used_days == Decimal("0.00")
    assert balance.available_days == Decimal("2.00")


async def test_cancelling_a_pending_request_refunds_days(session, seeded, next_monday):
    request = await leave_request_service.create_leave_request(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=_payload(seeded["leave_type"].id, next_monday),
        context=CONTEXT,
    )
    await session.commit()

    await leave_request_service.cancel_leave_request(
        session,
        leave_request=request,
        actor=seeded["employee"],
        reason="Plans changed",
        context=CONTEXT,
    )
    await session.commit()

    assert request.status == LeaveStatus.CANCELLED
    balance = await session.get(LeaveBalance, seeded["balance"].id)
    await session.refresh(balance)
    assert balance.available_days == Decimal("2.00")


async def test_estimate_does_not_mutate_the_balance(session, seeded, next_monday):
    from app.schemas.leave_request import LeaveEstimateRequest

    estimate = await leave_request_service.estimate(
        session,
        user=seeded["employee"],
        tenant=seeded["tenant"],
        payload=LeaveEstimateRequest(
            leave_type_id=seeded["leave_type"].id,
            start_date=next_monday,
            end_date=next_monday + timedelta(days=1),
        ),
    )
    assert estimate.working_days == Decimal("2.00")
    assert estimate.available_balance == Decimal("2.00")
    assert estimate.balance_after == Decimal("0.00")

    balance = await session.get(LeaveBalance, seeded["balance"].id)
    await session.refresh(balance)
    assert balance.pending_days == Decimal("0.00")
