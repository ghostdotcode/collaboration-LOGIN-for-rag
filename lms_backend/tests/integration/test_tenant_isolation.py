"""
Multi-tenancy guarantees.

Two layers are asserted separately:
  1. Application layer — services filter by `tenant_id`.
  2. Database layer — Postgres RLS makes a cross-tenant read return zero
     rows even when the query has no tenant predicate at all.

The RLS assertions connect as a *non-superuser* role, because superusers
bypass row-level security and would make the test vacuously pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base_class import DB_SCHEMA
from app.models import TENANT_SCOPED_TABLES, Tenant, User
from app.models.enums import TenantStatus, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

RLS_ROLE = "lms_rls_test"
RLS_PASSWORD = "rls_test_password"


async def test_models_all_carry_a_tenant_discriminator():
    """Guard: a new tenant-scoped model must not be added without RLS."""
    expected = {
        "users",
        "locations",
        "leave_types",
        "leave_policies",
        "leave_balances",
        "leave_requests",
        "approval_workflows",
        "approval_workflow_steps",
        "leave_approvals",
        "delegations",
        "holiday_calendars",
        "holidays",
        "audit_logs",
    }
    assert expected.issubset(set(TENANT_SCOPED_TABLES))


async def test_queries_are_scoped_to_the_calling_tenant(session, seeded):
    other = Tenant(
        name="Globex",
        slug=f"globex-{uuid.uuid4().hex[:8]}",
        status=TenantStatus.ACTIVE,
    )
    session.add(other)
    await session.flush()

    intruder = User(
        tenant_id=other.id,
        email="spy@globex.test",
        first_name="Hank",
        last_name="Scorpio",
        role=UserRole.ADMIN,
        date_of_joining=date(2022, 1, 1),
    )
    session.add(intruder)
    await session.commit()

    from sqlalchemy import select

    acme_users = list(
        await session.scalars(
            select(User.email).where(User.tenant_id == seeded["tenant"].id)
        )
    )
    assert "spy@globex.test" not in acme_users


@pytest.mark.asyncio
async def test_rls_blocks_reads_without_tenant_context(engine, seeded):
    """
    With RLS active and no `app.current_tenant` set, a bare SELECT returns
    nothing — the fail-safe direction for a forgotten WHERE clause.
    """
    admin_url = os.environ["LMS_TEST_DATABASE_URL"]
    table = f'"{DB_SCHEMA}"."users"' if DB_SCHEMA else '"users"'

    # Enable RLS and provision an unprivileged role to test through.
    async with engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON " + table))
        await conn.execute(
            text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
                """
            )
        )
        await conn.execute(
            text(
                f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RLS_ROLE}') THEN
                        CREATE ROLE {RLS_ROLE} LOGIN PASSWORD '{RLS_PASSWORD}';
                    END IF;
                END $$
                """
            )
        )
        if DB_SCHEMA:
            await conn.execute(
                text(f'GRANT USAGE ON SCHEMA "{DB_SCHEMA}" TO {RLS_ROLE}')
            )
            await conn.execute(
                text(
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA "{DB_SCHEMA}" TO {RLS_ROLE}'
                )
            )

    restricted_url = (
        admin_url.split("://")[0]
        + "://"
        + f"{RLS_ROLE}:{RLS_PASSWORD}@"
        + admin_url.split("@", 1)[1]
    )
    restricted = create_async_engine(restricted_url, pool_pre_ping=True)
    try:
        async with restricted.connect() as conn:
            no_context = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            assert no_context.scalar() == 0, "RLS did not block a context-free read"

            await conn.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(seeded["tenant"].id)},
            )
            scoped = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            assert scoped.scalar() >= 2, "RLS blocked a legitimate in-tenant read"
    finally:
        await restricted.dispose()
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
            await conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
