"""
Authentication endpoints, including the SSO hand-off from the RAG chatbot.

Tenant resolution note: a bare email is ambiguous in a multi-tenant system
(the same consultant can exist in two workspaces) *and* Row-Level Security
means we cannot scan across tenants anyway. So every entry point resolves a
workspace first — from `tenant_slug`, the `X-Tenant-ID` header, or the sole
tenant in single-workspace deployments.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    ACCESS_COOKIE_NAME,
    get_current_user,
    get_request_context,
)
from app.api.dependencies.database import get_db
from app.api.dependencies.rate_limit import login_rate_limit
from app.core.config import settings
from app.core.context import RequestContext
from app.core.exceptions import (
    AuthenticationError,
    TenantInactiveError,
    ValidationFailedError,
)
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.session import set_tenant_context
from app.models.enums import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    SSOExchangeRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.schemas.user import UserRead
from app.services import audit_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


async def _resolve_tenant(
    session: AsyncSession, *, slug: Optional[str], request: Request
) -> Tenant:
    """Find the workspace this request is for, before any user lookup."""
    if slug:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            raise AuthenticationError("Unknown workspace.")
        return tenant

    header_value = request.headers.get(settings.TENANT_HEADER)
    if header_value:
        try:
            tenant = await session.get(Tenant, uuid.UUID(header_value))
        except (ValueError, TypeError):
            tenant = None
        if tenant is None:
            raise AuthenticationError("Unknown workspace.")
        return tenant

    # Single-workspace deployment (the common self-hosted case).
    count = await session.scalar(select(func.count(Tenant.id)))
    if count == 1:
        tenant = await session.scalar(select(Tenant).limit(1))
        if tenant is not None:
            return tenant

    raise ValidationFailedError(
        "A workspace must be specified. Provide `tenant_slug` or the "
        f"{settings.TENANT_HEADER} header."
    )


def _set_session_cookies(response: Response, *, access: str, refresh: str) -> None:
    """
    Store tokens as HTTPOnly cookies.

    HTTPOnly keeps them out of reach of any XSS on the chatbot page, and
    SameSite=lax still allows the top-level navigation from the chatbot's
    "Apply for Leave" button.
    """
    common = {
        "httponly": True,
        "secure": settings.COOKIE_SECURE,
        "samesite": "lax",
        "domain": settings.COOKIE_DOMAIN,
        "path": "/",
    }
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **common,
    )
    response.set_cookie(
        settings.REFRESH_COOKIE_NAME,
        refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        **common,
    )


async def _issue_session(
    session: AsyncSession,
    response: Response,
    user: User,
    context: RequestContext,
) -> TokenResponse:
    access = create_access_token(
        subject=user.email,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role.value,
    )
    refresh = create_refresh_token(
        subject=user.email, user_id=user.id, tenant_id=user.tenant_id
    )
    _set_session_cookies(response, access=access, refresh=refresh)

    user.last_login_at = datetime.now(timezone.utc)
    await audit_service.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        actor_email=user.email,
        channel=context.channel,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        request_id=context.request_id,
    )
    return TokenResponse(
        access_token=access,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        full_name=user.full_name,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(login_rate_limit)],
    summary="Password login",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> TokenResponse:
    tenant = await _resolve_tenant(session, slug=payload.tenant_slug, request=request)
    await set_tenant_context(session, tenant.id)

    user = await session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.email == payload.email)
    )
    # Same error and roughly the same work either way, so the response can't
    # be used to enumerate which emails exist.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Invalid email or password.")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")
    if not tenant.can_transact:
        raise TenantInactiveError()

    return await _issue_session(session, response, user, context)


@router.post(
    "/sso/exchange",
    response_model=TokenResponse,
    dependencies=[Depends(login_rate_limit)],
    summary="Exchange a RAG chatbot token for an LMS session",
)
async def sso_exchange(
    payload: SSOExchangeRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> TokenResponse:
    """
    Zero-friction entry from the chatbot's "Apply for Leave" button.

    The chatbot's token is signed with the same secret, so verifying it is
    proof the user already authenticated moments ago — no second login.
    """
    claims = decode_token(payload.chatbot_token, expected_type=TokenType.ACCESS)
    email = claims.get("sub")
    if not email:
        raise AuthenticationError("The provided token has no subject.")

    tenant = await _resolve_tenant(session, slug=payload.tenant_slug, request=request)
    await set_tenant_context(session, tenant.id)

    user = await session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.email == email)
    )
    if user is None or not user.is_active:
        raise AuthenticationError(
            "You do not have a leave-management profile in this workspace yet. "
            "Ask HR to add you."
        )
    return await _issue_session(session, response, user, context)


@router.post("/refresh", response_model=TokenResponse, summary="Rotate access token")
async def refresh_session(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> TokenResponse:
    token = payload.refresh_token or request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not token:
        raise AuthenticationError("No refresh token was supplied.")

    claims = decode_token(token, expected_type=TokenType.REFRESH)
    tenant_id = claims.get("tid")
    user_id = claims.get("uid")
    if not tenant_id or not user_id:
        raise AuthenticationError()

    await set_tenant_context(session, uuid.UUID(str(tenant_id)))
    user = await session.get(User, uuid.UUID(str(user_id)))
    if user is None or not user.is_active:
        raise AuthenticationError()
    return await _issue_session(session, response, user, context)


@router.post("/logout", response_model=MessageResponse, summary="Clear session cookies")
async def logout(response: Response) -> MessageResponse:
    for name in (ACCESS_COOKIE_NAME, settings.REFRESH_COOKIE_NAME):
        response.delete_cookie(name, domain=settings.COOKIE_DOMAIN, path="/")
    return MessageResponse(message="Signed out.")


@router.get("/me", response_model=UserRead, summary="Current user profile")
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post(
    "/password",
    response_model=MessageResponse,
    dependencies=[Depends(login_rate_limit)],
    summary="Change password",
)
async def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> MessageResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthenticationError("Your current password is incorrect.")

    user.password_hash = hash_password(payload.new_password)
    await audit_service.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        actor_email=user.email,
        after={"password_hash": "***"},
        channel=context.channel,
        ip_address=context.ip_address,
        request_id=context.request_id,
    )
    return MessageResponse(message="Password updated.")
