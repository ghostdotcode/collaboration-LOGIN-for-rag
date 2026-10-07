"""
SQLAlchemy async engine, session factories and the tenant/RLS plumbing.

Two factories live here on purpose:
  * `AsyncSessionLocal` — the API path (asyncpg, non-blocking).
  * `SyncSessionLocal`  — Celery workers and Alembic, which are sync.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# Session-scoped GUC that the RLS policies read.
TENANT_GUC = "app.current_tenant"

engine: AsyncEngine = create_async_engine(
    settings.async_database_url,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def set_tenant_context(
    session: AsyncSession, tenant_id: Optional[uuid.UUID]
) -> None:
    """
    Pin the current transaction to one tenant.

    `set_config(..., true)` makes the setting transaction-local, so a pooled
    connection can never leak one tenant's context into the next request.
    Also bounds how long `SELECT ... FOR UPDATE` will wait, so a stuck lock
    surfaces as a fast 409 rather than a hung worker.
    """
    if tenant_id is not None:
        await session.execute(
            text(f"SELECT set_config('{TENANT_GUC}', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
    await session.execute(
        text(f"SET LOCAL lock_timeout = '{settings.DB_LOCK_TIMEOUT_MS}ms'")
    )


@asynccontextmanager
async def session_scope(
    tenant_id: Optional[uuid.UUID] = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Transactional scope for non-request callers (scripts, startup tasks).

    Commits on success, rolls back on any exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            if tenant_id is not None:
                await set_tenant_context(session, tenant_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def database_healthy() -> bool:
    """Readiness probe helper — cheap round trip, never raises."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("database health check failed: %s", exc)
        return False


async def dispose_engine() -> None:
    """Close pooled connections on shutdown so pods terminate cleanly."""
    await engine.dispose()


# ── Sync access for Celery workers / Alembic ───────────────────────────────


def get_sync_session_factory():
    """
    Build a sync sessionmaker lazily.

    Kept lazy so importing this module (which the API does at boot) never
    creates a second, unused psycopg2 pool.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_engine = create_engine(
        settings.sync_database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    return sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False)
