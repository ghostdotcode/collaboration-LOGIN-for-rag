"""
Redis-backed sliding-window rate limiting, per IP and per tenant.

Implemented with a sorted set of request timestamps rather than a fixed
counter, so a client can't burst 2x the limit by straddling a window
boundary. Pipelined into a single round trip.

Failure mode is deliberately **fail-open**: if Redis is unreachable we log
and allow the request. Availability of leave submission matters more than
perfect throttling, and the WAF layer (per the spec) is the outer defence.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.redis import get_redis

logger = logging.getLogger(__name__)


async def _consume(key: str, limit: int, window_seconds: int) -> Optional[int]:
    """
    Record a hit and return the count in the current window.

    Returns None when Redis is unavailable (caller should allow the request).
    """
    client = get_redis()
    if client is None:
        return None

    now = time.time()
    cutoff = now - window_seconds
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 1)
        results = await pipe.execute()
        return int(results[2])
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("rate limiter degraded (allowing request): %s", exc)
        return None


class RateLimit:
    """
    Route dependency enforcing a per-IP and per-tenant budget.

    Usage:
        @router.post("/leaves", dependencies=[Depends(RateLimit(limit=30))])
    """

    def __init__(
        self,
        *,
        limit: Optional[int] = None,
        window_seconds: Optional[int] = None,
        scope: str = "default",
    ) -> None:
        self.limit = limit or settings.RATE_LIMIT_PER_IP
        self.window_seconds = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
        self.scope = scope

    async def __call__(self, request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "unknown")
        )

        ip_count = await _consume(
            f"rl:{self.scope}:ip:{client_ip}", self.limit, self.window_seconds
        )
        if ip_count is not None and ip_count > self.limit:
            raise RateLimitExceededError(
                "Too many requests from this address.",
                retry_after=self.window_seconds,
            )

        # Tenant budget stops one noisy customer from starving the others.
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id:
            tenant_count = await _consume(
                f"rl:{self.scope}:tenant:{tenant_id}",
                settings.RATE_LIMIT_PER_TENANT,
                self.window_seconds,
            )
            if (
                tenant_count is not None
                and tenant_count > settings.RATE_LIMIT_PER_TENANT
            ):
                raise RateLimitExceededError(
                    "Your workspace has exceeded its request budget.",
                    retry_after=self.window_seconds,
                )


# Tighter budget for credential endpoints (brute-force defence).
login_rate_limit = RateLimit(limit=10, window_seconds=60, scope="auth")
write_rate_limit = RateLimit(limit=60, window_seconds=60, scope="write")
