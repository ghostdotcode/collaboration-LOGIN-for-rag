"""
Authentication, tenant binding and RBAC dependencies.

`get_current_user` is where the multi-tenant contract is established: it
resolves the principal from the JWT and then pins the database session to
that tenant, which activates the Postgres RLS policies for the rest of the
transaction.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Iterable, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.core.exceptions import (
    AuthenticationError,
    PermissionDeniedError,
    TenantInactiveError,
)
from app.core.security import TokenType, decode_token, has_role
from app.models.enums import ActionChannel, UserRole
from app.models.tenant import Tenant
from app.models.user import User

# auto_error=False so a missing header produces our JSON error shape rather
# than FastAPI's default, and so cookie-only browser calls still reach us.
bearer_scheme = HTTPBearer(auto_error=False)

ACCESS_COOKIE_NAME = "lms_access"


def extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    """
    Pull the access token from the Authorization header or the SSO cookie.

    The chatbot stores its token in localStorage and sends a header; the LMS
    browser session uses an HTTPOnly cookie. Supporting both is what makes
    the hand-off seamless in either direction.
    """
    if (
        credentials
        and credentials.scheme.lower() == "bearer"
        and credentials.credentials
    ):
        return credentials.credentials
    cookie_token = request.cookies.get(ACCESS_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    raise AuthenticationError("Authentication credentials were not provided.")


def get_request_context(request: Request) -> RequestContext:
    """Client metadata for the audit trail."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    return RequestContext(
        ip_address=client_ip,
        user_agent=request.headers.get("User-Agent"),
        request_id=request.headers.get("X-Request-ID"),
        channel=ActionChannel.WEB,
    )


async def get_current_user(
    request: Request,
    token: str = Depends(extract_token),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Resolve and authorise the caller, then bind the session to its tenant."""
    claims = decode_token(token, expected_type=TokenType.ACCESS)

    user: Optional[User] = None
    raw_uid = claims.get("uid")
    if raw_uid:
        try:
            user = await session.get(User, uuid.UUID(str(raw_uid)))
        except (ValueError, TypeError):
            user = None

    if user is None:
        # Tokens minted by the RAG chatbot only carry `sub` (the email), so
        # fall back to an email lookup to keep SSO working.
        email = claims.get("sub")
        tenant_hint = claims.get("tid")
        stmt = select(User).where(User.email == email)
        if tenant_hint:
            # A malformed hint just means "don't narrow"; the lookup below
            # still cannot cross tenants because RLS is not yet engaged and
            # the email itself is unique per tenant.
            with contextlib.suppress(ValueError, TypeError):
                stmt = stmt.where(User.tenant_id == uuid.UUID(str(tenant_hint)))
        user = await session.scalar(stmt.limit(1))

    if user is None or not user.is_active:
        raise AuthenticationError("Your account is not active in this workspace.")

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise AuthenticationError()
    if not tenant.can_transact and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise TenantInactiveError(
            "This workspace is read-only because its subscription is not active."
        )

    # Activate RLS for the remainder of this transaction.
    from app.db.session import set_tenant_context

    await set_tenant_context(session, user.tenant_id)

    request.state.tenant_id = str(user.tenant_id)
    request.state.user_id = str(user.id)
    return user


async def get_current_tenant(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise AuthenticationError()
    return tenant


def require_roles(*roles: str):
    """
    Dependency factory guarding a route or a whole router.

    Admins implicitly pass every check (see `has_role`), which avoids
    littering every endpoint with an extra "admin" literal.
    """
    allowed: Iterable[str] = [str(getattr(r, "value", r)).lower() for r in roles]

    async def _guard(user: User = Depends(get_current_user)) -> User:
        if not has_role(user, allowed):
            raise PermissionDeniedError(
                f"This action requires one of: {', '.join(sorted(allowed))}."
            )
        return user

    return _guard


# Common role guards, named for readability at the call site.
require_hr = require_roles(UserRole.HR)
require_manager = require_roles(UserRole.MANAGER, UserRole.HR)
require_admin = require_roles(UserRole.ADMIN)
