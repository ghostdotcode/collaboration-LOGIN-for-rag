"""
Policy immutability.

The scenario from the spec: HR cuts annual leave from 20 days to 15 in
October. Balances accrued January-September must keep pointing at the old
version, and the new version must apply only from its effective date.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationFailedError
from app.schemas.leave_policy import LeavePolicyCreate
from app.services import policy_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_new_version_closes_the_previous_one(session, seeded, today):
    tenant = seeded["tenant"]
    original = seeded["policy"]
    assert original.version == 1
    assert original.effective_to is None

    cutover = date(today.year, 10, 1)
    updated = await policy_service.create_policy_version(
        session,
        tenant_id=tenant.id,
        payload=LeavePolicyCreate(
            leave_type_id=seeded["leave_type"].id,
            name="Annual Leave (reduced)",
            effective_from=cutover,
            annual_quota_days=Decimal("15.00"),
            accrual_rate_days=Decimal("1.25"),
        ),
        actor=seeded["manager"],
    )
    await session.commit()
    await session.refresh(original)

    # Version 1 is closed the day before, not rewritten.
    assert updated.version == 2
    assert updated.previous_version_id == original.id
    assert original.effective_to == date(today.year, 9, 30)
    assert original.annual_quota_days == Decimal("20.00")


async def test_resolution_is_date_sensitive(session, seeded, today):
    cutover = date(today.year, 10, 1)
    await policy_service.create_policy_version(
        session,
        tenant_id=seeded["tenant"].id,
        payload=LeavePolicyCreate(
            leave_type_id=seeded["leave_type"].id,
            name="Annual Leave (reduced)",
            effective_from=cutover,
            annual_quota_days=Decimal("15.00"),
            accrual_rate_days=Decimal("1.25"),
        ),
        actor=seeded["manager"],
    )
    await session.commit()

    before = await policy_service.resolve_policy(
        session,
        tenant_id=seeded["tenant"].id,
        leave_type_id=seeded["leave_type"].id,
        on_date=date(today.year, 6, 15),
        user=seeded["employee"],
    )
    after = await policy_service.resolve_policy(
        session,
        tenant_id=seeded["tenant"].id,
        leave_type_id=seeded["leave_type"].id,
        on_date=date(today.year, 11, 15),
        user=seeded["employee"],
    )

    assert before is not None and before.annual_quota_days == Decimal("20.00")
    assert after is not None and after.annual_quota_days == Decimal("15.00")


async def test_backdating_a_version_is_rejected(session, seeded, today):
    with pytest.raises(ValidationFailedError):
        await policy_service.create_policy_version(
            session,
            tenant_id=seeded["tenant"].id,
            payload=LeavePolicyCreate(
                leave_type_id=seeded["leave_type"].id,
                name="Sneaky retroactive change",
                # Before version 1's effective_from: would corrupt history.
                effective_from=date(today.year - 1, 1, 1),
                annual_quota_days=Decimal("5.00"),
                accrual_rate_days=Decimal("0.42"),
            ),
            actor=seeded["manager"],
        )


async def test_version_history_is_ordered(session, seeded, today):
    for month, quota in ((4, "18.00"), (10, "15.00")):
        await policy_service.create_policy_version(
            session,
            tenant_id=seeded["tenant"].id,
            payload=LeavePolicyCreate(
                leave_type_id=seeded["leave_type"].id,
                name=f"Annual Leave v{month}",
                effective_from=date(today.year, month, 1),
                annual_quota_days=Decimal(quota),
                accrual_rate_days=Decimal("1.25"),
            ),
            actor=seeded["manager"],
        )
    await session.commit()

    versions = await policy_service.list_versions(
        session,
        tenant_id=seeded["tenant"].id,
        leave_type_id=seeded["leave_type"].id,
    )
    assert [v.version for v in versions] == [3, 2, 1]
    # Exactly one open version at any time.
    assert sum(1 for v in versions if v.effective_to is None) == 1
