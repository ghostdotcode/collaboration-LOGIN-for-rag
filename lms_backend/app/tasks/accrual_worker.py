"""
Nightly leave accrual and year-end rollover expiry.

The scaling problem (from the spec): at 00:00 on 1 January the system must
recompute balances for hundreds of thousands of employees across every
tenant. A single loop cannot do that inside a maintenance window.

The shape here is a fan-out: `run_nightly_accruals` does nothing but slice
user ids into chunks of `ACCRUAL_CHUNK_SIZE` and publish one task per chunk,
which RabbitMQ spreads across every available worker. Each chunk commits on
its own, so a failure re-runs 1,000 users rather than 500,000.

Idempotency is what makes that safe: `accrued_days` is *recomputed* from the
policy for a given `as_of` date, never incremented. Re-running the job — or
running it twice from two pods — converges on the same number.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select, text

from app.core.config import settings
from app.models.leave_balance import LeaveBalance
from app.models.leave_type import LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.services.leave_calculator import (
    ZERO,
    AccrualRule,
    accrued_days_as_of,
    quantize,
    resolve_leave_year,
    rollover_expiry_date,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _session_factory():
    """Sync session factory (Celery workers are not async)."""
    from app.db.session import get_sync_session_factory

    return get_sync_session_factory()


def _set_tenant(session, tenant_id) -> None:
    """Activate RLS for this transaction, exactly as the API does."""
    session.execute(
        text("SELECT set_config('app.current_tenant', :tid, true)"),
        {"tid": str(tenant_id)},
    )


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@celery_app.task(name="app.tasks.accrual_worker.run_nightly_accruals")
def run_nightly_accruals(as_of: Optional[str] = None) -> Dict[str, int]:
    """
    Fan out accrual work across all tenants. Dispatch only — no maths here.

    Triggered by the Kubernetes CronJob (and by Celery Beat as a backstop).
    """
    reference = (
        date.fromisoformat(as_of) if as_of else datetime.now(timezone.utc).date()
    )
    Session = _session_factory()
    dispatched = 0
    tenants_processed = 0

    with Session() as session:
        tenant_ids = list(session.scalars(select(Tenant.id)))

        for tenant_id in tenant_ids:
            _set_tenant(session, tenant_id)
            user_ids = [
                str(uid)
                for uid in session.scalars(
                    select(User.id).where(
                        User.tenant_id == tenant_id, User.is_active.is_(True)
                    )
                )
            ]
            if not user_ids:
                continue
            tenants_processed += 1
            for chunk in _chunks(user_ids, settings.ACCRUAL_CHUNK_SIZE):
                accrue_user_chunk.delay(
                    tenant_id=str(tenant_id),
                    user_ids=list(chunk),
                    as_of=reference.isoformat(),
                )
                dispatched += 1

    logger.info(
        "accrual fan-out complete tenants=%s chunks=%s as_of=%s",
        tenants_processed,
        dispatched,
        reference,
    )
    return {
        "tenants": tenants_processed,
        "chunks_dispatched": dispatched,
        "chunk_size": settings.ACCRUAL_CHUNK_SIZE,
    }


@celery_app.task(
    name="app.tasks.accrual_worker.accrue_user_chunk",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=5,
)
def accrue_user_chunk(
    self, tenant_id: str, user_ids: List[str], as_of: str
) -> Dict[str, int]:
    """
    Recompute accrued days for up to `ACCRUAL_CHUNK_SIZE` employees.

    Retries with exponential backoff: a transient database failover should
    not silently skip a thousand people's accrual.
    """
    reference = date.fromisoformat(as_of)
    Session = _session_factory()
    updated = 0
    skipped = 0

    from app.models.leave_policy import LeavePolicy

    with Session() as session:
        _set_tenant(session, tenant_id)
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return {"updated": 0, "skipped": len(user_ids)}

        leave_year = resolve_leave_year(reference, tenant.fiscal_year_start_month)
        leave_types = list(
            session.scalars(
                select(LeaveType).where(
                    LeaveType.tenant_id == tenant_id, LeaveType.is_active.is_(True)
                )
            )
        )

        for user_id in user_ids:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                skipped += 1
                continue

            for leave_type in leave_types:
                policy = session.scalar(
                    select(LeavePolicy)
                    .where(
                        LeavePolicy.tenant_id == tenant_id,
                        LeavePolicy.leave_type_id == leave_type.id,
                        LeavePolicy.effective_from <= reference,
                    )
                    .order_by(
                        LeavePolicy.effective_from.desc(), LeavePolicy.version.desc()
                    )
                    .limit(1)
                )
                if policy is None or not policy.is_in_force_on(reference):
                    continue

                balance = session.scalar(
                    select(LeaveBalance).where(
                        LeaveBalance.tenant_id == tenant_id,
                        LeaveBalance.user_id == user.id,
                        LeaveBalance.leave_type_id == leave_type.id,
                        LeaveBalance.year == leave_year.key,
                    )
                )
                if balance is None:
                    balance = LeaveBalance(
                        tenant_id=tenant.id,
                        user_id=user.id,
                        leave_type_id=leave_type.id,
                        year=leave_year.key,
                        policy_id=policy.id,
                    )
                    session.add(balance)

                if balance.last_accrued_on == reference:
                    continue  # already done today

                balance.accrued_days = accrued_days_as_of(
                    AccrualRule.from_policy(policy),
                    user.date_of_joining,
                    reference,
                    leave_year,
                    exit_date=user.date_of_exit,
                )
                balance.policy_id = policy.id
                balance.last_accrued_on = reference
                if balance.rolled_over_days > 0 and balance.rollover_expires_on is None:
                    balance.rollover_expires_on = rollover_expiry_date(
                        leave_year, policy.rollover_expiry_months
                    )
                updated += 1

        session.commit()

    logger.info(
        "accrued chunk tenant=%s users=%s updated=%s", tenant_id, len(user_ids), updated
    )
    return {"updated": updated, "skipped": skipped}


@celery_app.task(name="app.tasks.accrual_worker.expire_rollovers")
def expire_rollovers(as_of: Optional[str] = None) -> Dict[str, int]:
    """
    Lapse carried-over days whose grace window has closed.

    Only the *unspent* portion lapses — days already taken from the carry-over
    were used legitimately before the deadline.
    """
    reference = (
        date.fromisoformat(as_of) if as_of else datetime.now(timezone.utc).date()
    )
    Session = _session_factory()
    expired_rows = 0

    with Session() as session:
        tenant_ids = list(session.scalars(select(Tenant.id)))
        for tenant_id in tenant_ids:
            _set_tenant(session, tenant_id)
            balances = list(
                session.scalars(
                    select(LeaveBalance)
                    .where(
                        LeaveBalance.tenant_id == tenant_id,
                        LeaveBalance.rollover_expires_on.is_not(None),
                        LeaveBalance.rollover_expires_on < reference,
                        LeaveBalance.rolled_over_days > 0,
                    )
                    .with_for_update()
                )
            )
            for balance in balances:
                unused = min(
                    balance.rolled_over_days, max(balance.available_days, ZERO)
                )
                if unused <= 0:
                    balance.rollover_expires_on = None
                    continue
                balance.expired_days = quantize(balance.expired_days + unused)
                balance.rollover_expires_on = None
                expired_rows += 1
            session.commit()

    logger.info("rollover expiry complete rows=%s as_of=%s", expired_rows, reference)
    return {"expired": expired_rows}
