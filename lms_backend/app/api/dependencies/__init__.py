"""Reusable FastAPI dependencies (auth, DB sessions, rate limiting)."""

from app.api.dependencies.auth import (
    get_current_tenant,
    get_current_user,
    get_request_context,
    require_admin,
    require_hr,
    require_manager,
    require_roles,
)
from app.api.dependencies.database import get_db
from app.api.dependencies.rate_limit import (
    RateLimit,
    login_rate_limit,
    write_rate_limit,
)

__all__ = [
    "RateLimit",
    "get_current_tenant",
    "get_current_user",
    "get_db",
    "get_request_context",
    "login_rate_limit",
    "require_admin",
    "require_hr",
    "require_manager",
    "require_roles",
    "write_rate_limit",
]
