"""
Fixtures for database-backed tests.

These require a real PostgreSQL instance (row locking, RLS and `FOR UPDATE`
semantics cannot be faked on SQLite), so the whole module skips unless
`LMS_TEST_DATABASE_URL` is set. CI provides one; see
`.github/workflows/lms-backend-ci.yml`.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

DATABASE_URL = os.getenv("LMS_TEST_DATABASE_URL")

if not DATABASE_URL:
    pytest.skip(
        "LMS_TEST_DATABASE_URL is not set; skipping database integration tests",
        allow_module_level=True,
    )

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.base_class import DB_SCHEMA  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    LeaveBalance,
    LeavePolicy,
    LeaveType,
    Location,
    Tenant,
    User,
)
from app.models.enums import AccrualFrequency, TenantStatus, UserRole  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with eng.begin() as conn:
        if DB_SCHEMA:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as db:
        yield db
        await db.rollback()


@pytest.fixture
def today() -> date:
    return date.today()


@pytest_asyncio.fixture
async def seeded(session, today):
    """
    A minimal but realistic workspace:
      tenant -> location -> manager + employee -> leave type -> policy
      -> a balance with exactly 2 available days.

    The tight balance is deliberate: it makes the insufficient-balance and
    double-booking assertions unambiguous.
    """
    tenant = Tenant(
        name="Acme Corp",
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        status=TenantStatus.ACTIVE,
        employee_limit=50,
    )
    session.add(tenant)
    await session.flush()

    location = Location(
        tenant_id=tenant.id,
        code="HQ",
        name="Headquarters",
        country_code="US",
        timezone="America/New_York",
        working_days=[0, 1, 2, 3, 4],
    )
    session.add(location)
    await session.flush()

    manager = User(
        tenant_id=tenant.id,
        email="manager@acme.example",
        first_name="Mary",
        last_name="Manager",
        role=UserRole.MANAGER,
        location_id=location.id,
        date_of_joining=date(2020, 1, 1),
        password_hash=hash_password("correct-horse-battery"),
    )
    session.add(manager)
    await session.flush()

    employee = User(
        tenant_id=tenant.id,
        email="employee@acme.example",
        first_name="Eve",
        last_name="Employee",
        role=UserRole.EMPLOYEE,
        manager_id=manager.id,
        location_id=location.id,
        date_of_joining=date(2021, 6, 1),
        password_hash=hash_password("correct-horse-battery"),
    )
    session.add(employee)

    leave_type = LeaveType(
        tenant_id=tenant.id,
        code="ANNUAL",
        name="Annual Leave",
        min_notice_days=0,
        allow_retroactive=False,
    )
    session.add(leave_type)
    await session.flush()

    policy = LeavePolicy(
        tenant_id=tenant.id,
        leave_type_id=leave_type.id,
        name="Annual Leave 2026",
        version=1,
        effective_from=date(today.year, 1, 1),
        annual_quota_days=Decimal("20.00"),
        accrual_frequency=AccrualFrequency.MONTHLY,
        accrual_rate_days=Decimal("1.67"),
    )
    session.add(policy)
    await session.flush()

    balance = LeaveBalance(
        tenant_id=tenant.id,
        user_id=employee.id,
        leave_type_id=leave_type.id,
        year=today.year,
        policy_id=policy.id,
        opening_days=Decimal("2.00"),
    )
    session.add(balance)
    await session.commit()

    return {
        "tenant": tenant,
        "location": location,
        "manager": manager,
        "employee": employee,
        "leave_type": leave_type,
        "policy": policy,
        "balance": balance,
    }


@pytest.fixture
def next_monday(today) -> date:
    """A predictable working day, so weekend handling can't skew assertions."""
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
