"""
LMS Backend — FastAPI application entrypoint.

Wires the v1 routers, the global error handlers, CORS for the RAG chatbot
origin, and the K8s health probes.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import health
from app.api.routes.v1 import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.redis import close_redis
from app.db.session import dispose_engine

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. Connections are pooled lazily, so boot stays fast."""
    logger.info(
        "starting %s (env=%s, schema=%s)",
        settings.PROJECT_NAME,
        settings.ENVIRONMENT,
        settings.DB_SCHEMA,
    )
    yield
    await dispose_engine()
    await close_redis()
    logger.info("shutdown complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description=(
        "Enterprise Leave Management System. Multi-tenant, RLS-isolated, and "
        "SSO-integrated with the HR Policy RAG chatbot."
    ),
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    lifespan=lifespan,
)

register_exception_handlers(app)

# Credentials are cookies, so the allowed origins must be explicit — a
# wildcard is invalid with allow_credentials and would break the SSO flow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        settings.TENANT_HEADER,
        "X-Request-ID",
    ],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

if settings.is_production:
    # Blocks Host-header spoofing behind the ingress.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["*.meritech.example", "meritech.example"]
    )


@app.middleware("http")
async def request_id_and_security_headers(request: Request, call_next):
    """Correlate logs with a request id and set baseline security headers."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    response = await call_next(request)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


app.include_router(health.router)
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health", tags=["Health"], summary="Simple health check")
async def health_check() -> dict:
    """Kept for backwards compatibility; probes use /health/live and /ready."""
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT,
    }
