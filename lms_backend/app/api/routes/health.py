"""
Kubernetes probe endpoints.

  * `/health/live`  — is the process alive? Never touches dependencies, so a
    database blip doesn't get the pod killed and restarted pointlessly.
  * `/health/ready` — may it receive traffic? Checks Postgres and Redis, so a
    pod with a broken dependency is pulled from the Service's endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.core.redis import redis_healthy
from app.db.session import database_healthy

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live", summary="Liveness probe")
async def live() -> dict:
    return {"status": "alive", "service": settings.PROJECT_NAME}


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict:
    database_ok = await database_healthy()
    cache_ok = await redis_healthy()

    checks = {"database": database_ok, "redis": cache_ok}
    # Redis degrades gracefully (rate limiting fails open), so it must not
    # take the pod out of rotation; the database genuinely must be there.
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if database_ok else "not_ready",
        "checks": checks,
        "environment": settings.ENVIRONMENT,
    }
