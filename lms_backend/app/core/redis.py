"""
Lazily-constructed Redis client.

Imported by the rate limiter and the readiness probe. `redis` is an optional
import so the API still boots (degraded) in environments where caching isn't
provisioned yet.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Optional[Any] = None
_unavailable = False


def get_redis() -> Optional[Any]:
    """Return a shared async Redis client, or None if it can't be created."""
    global _client, _unavailable
    if _client is not None or _unavailable:
        return _client
    try:
        from redis.asyncio import Redis

        _client = Redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("redis client unavailable: %s", exc)
        _unavailable = True
    return _client


async def redis_healthy() -> bool:
    """Readiness probe helper — never raises."""
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("redis health check failed: %s", exc)
        return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):  # best effort on shutdown
            await _client.aclose()
        _client = None
