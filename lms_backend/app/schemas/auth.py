"""Authentication payloads (login, token refresh, SSO hand-off)."""

from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import UserRole
from app.schemas.common import Password, StrictSchema


class LoginRequest(StrictSchema):
    email: EmailStr
    # Not `Password`: legacy accounts may hold shorter secrets, and rejecting
    # them at the schema layer would leak which passwords are non-compliant.
    password: str = Field(min_length=1, max_length=72)
    tenant_slug: Optional[str] = Field(default=None, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole
    full_name: str


class RefreshRequest(StrictSchema):
    """Only used by non-browser clients; browsers send the HTTPOnly cookie."""

    refresh_token: Optional[str] = None


class SSOExchangeRequest(StrictSchema):
    """
    Exchange a RAG-chatbot access token for an LMS session.

    This is what makes the 'Apply for Leave' button a zero-click transition:
    the chatbot's token is already signed with the shared secret, so we
    verify it and mint LMS tokens for the matching employee.
    """

    chatbot_token: str = Field(min_length=16, max_length=4096)
    tenant_slug: Optional[str] = Field(default=None, max_length=64)


class PasswordChangeRequest(StrictSchema):
    current_password: str = Field(min_length=1, max_length=72)
    new_password: Password


class TokenClaims(BaseModel):
    """Decoded JWT claims, as used by the auth dependency."""

    sub: str
    uid: Optional[uuid.UUID] = None
    tid: Optional[uuid.UUID] = None
    role: Optional[str] = None
    typ: str = "access"
