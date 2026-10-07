"""
Seed a demo workspace so the LMS has something to show.

Run after `alembic upgrade head`:

    python -m scripts.seed_demo

Idempotent: existing rows are reused, so re-running tops up rather than
duplicating. Pass `--reset` to delete the demo tenant first.

Sample leave requests go through `leave_request_service`/`approval_service`
rather than raw inserts, so the seeded data is the same shape the app would
produce — balances reserved, approval chains materialised, audit rows written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

# Allow `python scripts/seed_demo.py` as well as `python -m scripts.seed_demo`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.context import RequestContext
from app.core.security import hash_password
from app.db.session import (
    AsyncSessionLocal,
    dispose_engine,
    set_tenant_context,
)
from app.models.approval_workflow import (
    ApprovalWorkflow,
    ApprovalWorkflowStep,
)
from app.models.enums import (
    AccrualFrequency,
    ApproverType,
    DayPart,
    LeaveUnit,
    TenantStatus,
    UserRole,
)
from app.models.holiday_calendar import Holiday, HolidayCalendar
from app.models.leave_policy import LeavePolicy
from app.models.leave_request import LeaveRequest
from app.models.leave_type import LeaveType
from app.models.location import Location
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave_request import LeaveRequestCreate
from app.services import (
    approval_service,
    balance_service,
    leave_request_service,
)
from app.services.leave_calculator import resolve_leave_year

TENANT_SLUG = "acme"
DEMO_PASSWORD = "Password123!"

# Every seeded person. The manager relationship is wired up afterwards by
# email, so the order here does not matter.
PEOPLE = [
    ("admin@acme.example", "Ada", "Admin", UserRole.ADMIN, None),
    ("hr@acme.example", "Hari", "Menon", UserRole.HR, None),
    ("manager@acme.example", "Meera", "Nair", UserRole.MANAGER, "hr@acme.example"),
    (
        "employee@acme.example",
        "Evan",
        "Pillai",
        UserRole.EMPLOYEE,
        "manager@acme.example",
    ),
    (
        "priya@acme.example",
        "Priya",
        "Sharma",
        UserRole.EMPLOYEE,
        "manager@acme.example",
    ),
    ("rahul@acme.example", "Rahul", "Verma", UserRole.EMPLOYEE, "manager@acme.example"),
]

LEAVE_TYPES = [
    {
        "code": "ANNUAL",
        "name": "Annual Leave",
        "color_hex": "#6366f1",
        "min_notice_days": 3,
        "max_consecutive_days": 15,
    },
    {
        "code": "SICK",
        "name": "Sick Leave",
        "color_hex": "#ef4444",
        # A medical certificate is required beyond two days, and sickness is
        # the one type you are allowed to file after the fact.
        "attachment_required_above_days": Decimal("2"),
        "allow_retroactive": True,
        "cannot_club_with": ["ANNUAL"],
    },
    {
        "code": "CASUAL",
        "name": "Casual Leave",
        "color_hex": "#06b6d4",
        "max_consecutive_days": 3,
    },
    {
        "code": "UNPAID",
        "name": "Unpaid Leave",
        "color_hex": "#64748b",
        "is_paid": False,
    },
]

POLICIES = {
    "ANNUAL": {
        "annual_quota_days": Decimal("20"),
        "accrual_frequency": AccrualFrequency.MONTHLY,
        "accrual_rate_days": Decimal("1.67"),
        "max_rollover_days": Decimal("5"),
        "rollover_expiry_months": 3,
    },
    "SICK": {
        "annual_quota_days": Decimal("12"),
        "accrual_frequency": AccrualFrequency.NONE,
        "accrual_rate_days": Decimal("0"),
    },
    "CASUAL": {
        "annual_quota_days": Decimal("6"),
        "accrual_frequency": AccrualFrequency.QUARTERLY,
        "accrual_rate_days": Decimal("1.5"),
    },
    "UNPAID": {
        "annual_quota_days": Decimal("0"),
        "accrual_frequency": AccrualFrequency.NONE,
        "accrual_rate_days": Decimal("0"),
        "allow_negative_balance": True,
        "max_negative_days": Decimal("30"),
    },
}


def context() -> RequestContext:
    return RequestContext(ip_address="127.0.0.1", user_agent="seed-script")


def chatbot_token(email: str) -> str:
    """
    Mint a token shaped exactly like the RAG chatbot's (`sub` = email, 24h,
    no `typ` claim), so the SSO hand-off can be demoed without running the
    chatbot — which otherwise downloads an embedding model on first boot.
    """
    import jwt

    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    return jwt.encode(
        {"sub": email, "exp": int(expires.timestamp())},
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


async def reset(session: AsyncSession) -> None:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == TENANT_SLUG))
    if tenant is None:
        print("Nothing to reset.")
        return
    await set_tenant_context(session, tenant.id)

    # The audit ledger is append-only by trigger, and the cascade from
    # `tenants` would try to delete (and SET NULL) rows in it. Lifting the
    # trigger for the length of this transaction is a deliberate dev-only
    # escape hatch — it is why this script never belongs in production.
    audit_table = (
        f"{settings.DB_SCHEMA}.audit_logs" if settings.DB_SCHEMA else "audit_logs"
    )
    await session.execute(
        text(f"ALTER TABLE {audit_table} DISABLE TRIGGER audit_logs_no_mutation")
    )
    try:
        # Children cascade from the tenant FK, so one delete is enough.
        await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
    finally:
        await session.execute(
            text(f"ALTER TABLE {audit_table} ENABLE TRIGGER audit_logs_no_mutation")
        )
    await session.commit()
    print(f"Deleted tenant {TENANT_SLUG}.")


async def seed_tenant(session: AsyncSession) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == TENANT_SLUG))
    if tenant is None:
        tenant = Tenant(
            name="Acme Corp",
            slug=TENANT_SLUG,
            status=TenantStatus.ACTIVE,
            subscription_tier="growth",
            employee_limit=250,
            fiscal_year_start_month=1,
            default_timezone="Asia/Kolkata",
        )
        session.add(tenant)
        await session.flush()
    return tenant


async def seed_location(session: AsyncSession, tenant: Tenant) -> Location:
    location = await session.scalar(
        select(Location).where(Location.tenant_id == tenant.id, Location.code == "HQ")
    )
    if location is None:
        location = Location(
            tenant_id=tenant.id,
            code="HQ",
            name="Bengaluru HQ",
            country_code="IN",
            timezone="Asia/Kolkata",
            working_days=[0, 1, 2, 3, 4],
            standard_hours_per_day=8,
        )
        session.add(location)
        await session.flush()
    return location


async def seed_users(
    session: AsyncSession, tenant: Tenant, location: Location
) -> Dict[str, User]:
    users: Dict[str, User] = {}
    password_hash = hash_password(DEMO_PASSWORD)
    joined = date.today() - timedelta(days=400)

    for email, first, last, role, _ in PEOPLE:
        user = await session.scalar(
            select(User).where(User.tenant_id == tenant.id, User.email == email)
        )
        if user is None:
            user = User(
                tenant_id=tenant.id,
                email=email,
                first_name=first,
                last_name=last,
                role=role,
                location_id=location.id,
                timezone="Asia/Kolkata",
                # Staggered joining dates exercise the pro-rata accrual maths.
                date_of_joining=joined + timedelta(days=len(users) * 45),
                password_hash=password_hash,
                is_active=True,
            )
            session.add(user)
            await session.flush()
        users[email] = user

    for email, _, _, _, manager_email in PEOPLE:
        if manager_email:
            users[email].manager_id = users[manager_email].id
    await session.flush()
    return users


async def seed_leave_types(
    session: AsyncSession, tenant: Tenant
) -> Dict[str, LeaveType]:
    types: Dict[str, LeaveType] = {}
    for spec in LEAVE_TYPES:
        code = spec["code"]
        leave_type = await session.scalar(
            select(LeaveType).where(
                LeaveType.tenant_id == tenant.id, LeaveType.code == code
            )
        )
        if leave_type is None:
            leave_type = LeaveType(
                tenant_id=tenant.id,
                unit=LeaveUnit.DAY,
                **spec,
            )
            session.add(leave_type)
            await session.flush()
        types[code] = leave_type
    return types


async def seed_policies(
    session: AsyncSession, tenant: Tenant, types: Dict[str, LeaveType]
) -> Dict[str, LeavePolicy]:
    leave_year = resolve_leave_year(date.today(), tenant.fiscal_year_start_month)
    policies: Dict[str, LeavePolicy] = {}

    for code, spec in POLICIES.items():
        leave_type = types[code]
        policy = await session.scalar(
            select(LeavePolicy).where(
                LeavePolicy.tenant_id == tenant.id,
                LeavePolicy.leave_type_id == leave_type.id,
                LeavePolicy.version == 1,
            )
        )
        if policy is None:
            policy = LeavePolicy(
                tenant_id=tenant.id,
                leave_type_id=leave_type.id,
                name=f"{leave_type.name} {leave_year.key}",
                version=1,
                effective_from=leave_year.start,
                prorate_on_joining=True,
                **spec,
            )
            session.add(policy)
            await session.flush()
        policies[code] = policy
    return policies


async def seed_holidays(
    session: AsyncSession, tenant: Tenant, location: Location
) -> int:
    today = date.today()
    leave_year = resolve_leave_year(today, tenant.fiscal_year_start_month)

    calendar = await session.scalar(
        select(HolidayCalendar).where(
            HolidayCalendar.tenant_id == tenant.id,
            HolidayCalendar.year == leave_year.key,
        )
    )
    if calendar is None:
        calendar = HolidayCalendar(
            tenant_id=tenant.id,
            name=f"India {leave_year.key}",
            year=leave_year.key,
            country_code="IN",
            location_id=location.id,
        )
        session.add(calendar)
        await session.flush()

    # Dated relative to today so the "upcoming holidays" widget is always
    # populated, whenever the demo is run.
    wanted = [
        ("Founders' Day", today + timedelta(days=6), False),
        ("Harvest Festival", today + timedelta(days=19), False),
        ("Optional: Regional Holiday", today + timedelta(days=27), True),
        ("Year-End Shutdown", today + timedelta(days=54), False),
    ]
    created = 0
    for name, holiday_date, optional in wanted:
        exists = await session.scalar(
            select(Holiday.id).where(
                Holiday.calendar_id == calendar.id,
                Holiday.holiday_date == holiday_date,
                Holiday.name == name,
            )
        )
        if exists is None:
            session.add(
                Holiday(
                    tenant_id=tenant.id,
                    calendar_id=calendar.id,
                    name=name,
                    holiday_date=holiday_date,
                    is_optional=optional,
                )
            )
            created += 1
    await session.flush()
    return created


async def seed_workflows(
    session: AsyncSession, tenant: Tenant, users: Dict[str, User]
) -> None:
    """Two chains, so the 'most specific match wins' behaviour is visible."""
    specs = [
        (
            "Standard · manager approval",
            Decimal("0"),
            True,
            [ApproverType.REPORTING_MANAGER],
        ),
        (
            "Long leave · manager then HR",
            Decimal("5"),
            False,
            [ApproverType.REPORTING_MANAGER, ApproverType.HR],
        ),
    ]

    for name, min_days, is_default, steps in specs:
        workflow = await session.scalar(
            select(ApprovalWorkflow).where(
                ApprovalWorkflow.tenant_id == tenant.id, ApprovalWorkflow.name == name
            )
        )
        if workflow is not None:
            continue
        workflow = ApprovalWorkflow(
            tenant_id=tenant.id,
            name=name,
            min_duration_days=min_days,
            is_default=is_default,
        )
        session.add(workflow)
        await session.flush()
        for level, approver_type in enumerate(steps, start=1):
            session.add(
                ApprovalWorkflowStep(
                    tenant_id=tenant.id,
                    workflow_id=workflow.id,
                    level=level,
                    approver_type=approver_type,
                    is_mandatory=True,
                )
            )
    await session.flush()


async def seed_balances(
    session: AsyncSession,
    tenant: Tenant,
    users: Dict[str, User],
    types: Dict[str, LeaveType],
    policies: Dict[str, LeavePolicy],
) -> None:
    today = date.today()
    leave_year = resolve_leave_year(today, tenant.fiscal_year_start_month)

    for user in users.values():
        for code, leave_type in types.items():
            await balance_service.get_or_create_balance(
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                leave_type_id=leave_type.id,
                year=leave_year.key,
                policy=policies[code],
            )
            # Recompute accrual so balances reflect the elapsed year rather
            # than starting everyone at zero.
            await balance_service.sync_accrual(
                session,
                user=user,
                leave_type_id=leave_type.id,
                leave_year=leave_year,
                as_of=today,
                policy=policies[code],
            )
    await session.flush()


def next_weekday(start: date, offset_days: int) -> date:
    """Nudge a date forward to the next Monday-Friday day."""
    day = start + timedelta(days=offset_days)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


async def seed_requests(
    session: AsyncSession,
    tenant: Tenant,
    users: Dict[str, User],
    types: Dict[str, LeaveType],
) -> List[str]:
    """A small, realistic mix: one approved, one pending, one half-day."""
    existing = await session.scalar(
        select(LeaveRequest.id).where(LeaveRequest.tenant_id == tenant.id).limit(1)
    )
    if existing is not None:
        return ["leave requests already present — skipped"]

    today = date.today()
    manager = users["manager@acme.example"]
    notes: List[str] = []

    async def submit(
        user: User,
        code: str,
        start: date,
        end: date,
        reason: str,
        start_part: DayPart = DayPart.FULL_DAY,
        end_part: DayPart = DayPart.FULL_DAY,
    ) -> Optional[LeaveRequest]:
        payload = LeaveRequestCreate(
            leave_type_id=types[code].id,
            start_date=start,
            end_date=end,
            start_day_part=start_part,
            end_day_part=end_part,
            reason=reason,
            acknowledged_conflicts=True,
        )
        try:
            return await leave_request_service.create_leave_request(
                session, user=user, tenant=tenant, payload=payload, context=context()
            )
        except Exception as exc:
            notes.append(f"skipped {code} for {user.email}: {exc}")
            return None

    # Approved: Rahul is away next week, which makes the team calendar and the
    # conflict warning on the apply form non-empty.
    rahul_start = next_weekday(today, 7)
    rahul = await submit(
        users["rahul@acme.example"],
        "ANNUAL",
        rahul_start,
        next_weekday(rahul_start, 2),
        "Family wedding",
    )
    if rahul is not None:
        await approval_service.decide(
            session,
            leave_request=rahul,
            actor=manager,
            approve=True,
            comment="Enjoy!",
            context=context(),
        )
        notes.append("approved annual leave for rahul@acme.example")

    # Pending: lands in the manager's approval queue.
    priya_start = next_weekday(today, 10)
    if await submit(
        users["priya@acme.example"],
        "ANNUAL",
        priya_start,
        next_weekday(priya_start, 3),
        "Trek in the Nilgiris",
    ):
        notes.append("pending annual leave for priya@acme.example (manager queue)")

    # Half day, to show fractional deduction on the dashboard.
    evan_day = next_weekday(today, 4)
    if await submit(
        users["employee@acme.example"],
        "CASUAL",
        evan_day,
        evan_day,
        "Apartment handover",
        start_part=DayPart.FIRST_HALF,
        end_part=DayPart.FIRST_HALF,
    ):
        notes.append("pending half-day casual leave for employee@acme.example")

    await session.flush()
    return notes


async def main(do_reset: bool) -> None:
    async with AsyncSessionLocal() as session:
        if do_reset:
            await reset(session)

        tenant = await seed_tenant(session)
        await set_tenant_context(session, tenant.id)

        location = await seed_location(session, tenant)
        users = await seed_users(session, tenant, location)
        types = await seed_leave_types(session, tenant)
        policies = await seed_policies(session, tenant, types)
        holidays = await seed_holidays(session, tenant, location)
        await seed_workflows(session, tenant, users)
        await seed_balances(session, tenant, users, types, policies)
        notes = await seed_requests(session, tenant, users, types)

        await session.commit()

    demo_token = chatbot_token("employee@acme.example")

    print("\n" + "=" * 68)
    print(f"  Seeded workspace '{tenant.name}' (slug: {TENANT_SLUG})")
    print("=" * 68)
    print(f"  {len(users)} users · {len(types)} leave types · {holidays} new holidays")
    for note in notes:
        print(f"  - {note}")
    print(f"\n  Password for every account: {DEMO_PASSWORD}")
    for email, first, last, role, _ in PEOPLE:
        print(f"    {email:22} {role.value:9} {first} {last}")
    print("\n  To open the LMS without running the chatbot, paste this into the")
    print("  browser console on http://localhost:8000, then click Apply for Leave:")
    print(f"\n    localStorage.setItem('access_token', '{demo_token}');\n")
    print("  (valid 24h — re-run this script to mint a fresh one)")
    print("=" * 68 + "\n")

    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the demo tenant and everything under it first.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.reset))
