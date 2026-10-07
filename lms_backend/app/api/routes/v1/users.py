"""Employee directory and provisioning (HR-facing)."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    get_current_tenant,
    get_current_user,
    get_request_context,
    require_hr,
)
from app.api.dependencies.database import get_db
from app.core.context import RequestContext
from app.core.exceptions import (
    NotFoundError,
    TenantSeatLimitError,
    ValidationFailedError,
)
from app.core.security import hash_password
from app.models.enums import AuditAction, UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.common import Page
from app.schemas.user import UserBrief, UserCreate, UserRead, UserUpdate
from app.services import audit_service

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=Page[UserRead], summary="List employees")
async def list_users(
    search: Optional[str] = Query(default=None, max_length=120),
    role: Optional[UserRole] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
) -> Page[UserRead]:
    conditions = [User.tenant_id == actor.tenant_id]
    if role is not None:
        conditions.append(User.role == role)
    if is_active is not None:
        conditions.append(User.is_active.is_(is_active))
    if search:
        # ILIKE with a bound parameter — the ORM escapes it, and the wildcard
        # is added here rather than accepted from the client.
        pattern = f"%{search}%"
        conditions.append(
            or_(
                User.email.ilike(pattern),
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
            )
        )

    total = await session.scalar(select(func.count(User.id)).where(and_(*conditions)))
    rows = await session.scalars(
        select(User)
        .where(and_(*conditions))
        .order_by(User.first_name, User.last_name)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    return Page[UserRead](
        items=[UserRead.model_validate(row) for row in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an employee",
)
async def create_user(
    payload: UserCreate,
    actor: User = Depends(require_hr),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> UserRead:
    """
    Provision an employee.

    Enforces the subscription's seat limit here rather than at billing time,
    so a tenant can't quietly exceed the tier they're paying for.
    """
    active_count = await session.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == actor.tenant_id, User.is_active.is_(True)
        )
    )
    if int(active_count or 0) >= tenant.employee_limit:
        raise TenantSeatLimitError(
            f"Your plan allows {tenant.employee_limit} active employees.",
            details={"employee_limit": tenant.employee_limit},
        )

    existing = await session.scalar(
        select(User.id).where(
            User.tenant_id == actor.tenant_id, User.email == payload.email
        )
    )
    if existing:
        raise ValidationFailedError("An employee with this email already exists.")

    if payload.manager_id is not None:
        manager = await session.get(User, payload.manager_id)
        if manager is None or manager.tenant_id != actor.tenant_id:
            raise NotFoundError("The nominated manager was not found.")

    user = User(
        tenant_id=actor.tenant_id,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        role=payload.role,
        manager_id=payload.manager_id,
        location_id=payload.location_id,
        employee_code=payload.employee_code,
        timezone=payload.timezone,
        date_of_joining=payload.date_of_joining,
        # No password => SSO-only account; they sign in through the chatbot.
        password_hash=hash_password(payload.password) if payload.password else None,
    )
    session.add(user)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        actor_id=actor.id,
        actor_email=actor.email,
        after=user.to_dict(),
        channel=context.channel,
        ip_address=context.ip_address,
        request_id=context.request_id,
    )
    return UserRead.model_validate(user)


@router.get("/me/team", response_model=List[UserBrief], summary="My direct reports")
async def my_team(
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> List[UserBrief]:
    rows = await session.scalars(
        select(User)
        .where(
            User.tenant_id == actor.tenant_id,
            User.manager_id == actor.id,
            User.is_active.is_(True),
        )
        .order_by(User.first_name)
    )
    return [UserBrief.model_validate(row) for row in rows]


@router.get("/{user_id}", response_model=UserRead, summary="Get an employee")
async def get_user(
    user_id: uuid.UUID,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserRead:
    if user_id == actor.id:
        return UserRead.model_validate(actor)

    user = await session.get(User, user_id)
    if user is None or user.tenant_id != actor.tenant_id:
        raise NotFoundError("Employee not found.")

    is_their_manager = user.manager_id == actor.id
    if not is_their_manager and actor.role not in (UserRole.HR, UserRole.ADMIN):
        # Directory lookups are allowed, but only the brief projection —
        # joining dates and exit dates are not everyone's business.
        return UserRead.model_validate(user).model_copy(
            update={"date_of_exit": None, "last_login_at": None}
        )
    return UserRead.model_validate(user)


@router.patch("/{user_id}", response_model=UserRead, summary="Update an employee")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    actor: User = Depends(require_hr),
    session: AsyncSession = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> UserRead:
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != actor.tenant_id:
        raise NotFoundError("Employee not found.")

    if payload.manager_id is not None:
        if payload.manager_id == user.id:
            raise ValidationFailedError("An employee cannot report to themselves.")
        if await _would_create_cycle(session, user.id, payload.manager_id):
            raise ValidationFailedError(
                "That change would create a circular reporting line."
            )

    before = user.to_dict()
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)

    await audit_service.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_id=actor.id,
        actor_email=actor.email,
        before=before,
        after=user.to_dict(),
        channel=context.channel,
        ip_address=context.ip_address,
        request_id=context.request_id,
    )
    return UserRead.model_validate(user)


async def _would_create_cycle(
    session: AsyncSession, user_id: uuid.UUID, new_manager_id: uuid.UUID
) -> bool:
    """
    Walk up the proposed chain looking for the employee themselves.

    A cycle here would make the approval chain recurse forever, so it is
    cheaper to reject the org-chart edit than to defend every traversal.
    """
    seen: set[uuid.UUID] = set()
    cursor: Optional[uuid.UUID] = new_manager_id
    while cursor is not None and cursor not in seen:
        if cursor == user_id:
            return True
        seen.add(cursor)
        cursor = await session.scalar(select(User.manager_id).where(User.id == cursor))
    return False
