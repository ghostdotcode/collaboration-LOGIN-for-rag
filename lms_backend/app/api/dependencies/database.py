"""Request-scoped database session (one unit of work per request)."""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a session that commits on success and rolls back on any error.

    Endpoints therefore never call `commit()` themselves — a handler that
    raises after a partial write leaves nothing behind.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
