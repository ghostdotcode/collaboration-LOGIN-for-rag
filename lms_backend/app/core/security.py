"""
Password hashing, JWT issuing/verification and the RBAC decorator.

Compatibility note: password hashes and access tokens are wire-compatible
with the existing RAG chatbot (`/auth.py` at the repo root) — bcrypt for
hashes, HS256 JWT with `sub` = email — which is what lets a user walk from
the chatbot into the LMS without logging in again.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps
from typing import Any, Dict, Iterable, Optional

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError

# bcrypt silently truncates anything past 72 bytes; schemas enforce this too,
# but a hard guard here stops a truncation-based auth bypass.
MAX_PASSWORD_BYTES = 72


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"
    ACTION = "action"  # one-click approve/reject links in emails


# ── Passwords ──────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a plaintext password with a per-password random salt."""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: Optional[str]) -> bool:
    """Constant-time-ish password check that never raises on bad input."""
    if not password_hash:
        return False
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── JWT ────────────────────────────────────────────────────────────────────


def _encode(payload: Dict[str, Any], expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    to_encode = {
        **payload,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    *,
    subject: str,
    user_id: Optional[uuid.UUID] = None,
    tenant_id: Optional[uuid.UUID] = None,
    role: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Short-lived (15 min) access token. `subject` is the user's email."""
    payload: Dict[str, Any] = {"sub": subject, "typ": TokenType.ACCESS.value}
    if user_id:
        payload["uid"] = str(user_id)
    if tenant_id:
        payload["tid"] = str(tenant_id)
    if role:
        payload["role"] = role
    if extra:
        payload.update(extra)
    return _encode(payload, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(
    *,
    subject: str,
    user_id: Optional[uuid.UUID] = None,
    tenant_id: Optional[uuid.UUID] = None,
) -> str:
    """Long-lived (7 day) refresh token — only ever sent as an HTTPOnly cookie."""
    payload: Dict[str, Any] = {"sub": subject, "typ": TokenType.REFRESH.value}
    if user_id:
        payload["uid"] = str(user_id)
    if tenant_id:
        payload["tid"] = str(tenant_id)
    return _encode(payload, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def create_action_token(
    *,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
) -> str:
    """
    Single-purpose token for 'Approve' / 'Reject' buttons inside emails.

    Scoped to one leave request *and* one action, so a leaked 'approve' link
    can never be replayed as a reject (or against another request).
    """
    if action not in {"approve", "reject"}:
        raise ValueError("action must be 'approve' or 'reject'")
    payload = {
        "sub": str(approver_id),
        "typ": TokenType.ACTION.value,
        "tid": str(tenant_id),
        "rid": str(request_id),
        "act": action,
    }
    return _encode(payload, timedelta(hours=settings.ACTION_TOKEN_EXPIRE_HOURS))


def decode_token(
    token: str, *, expected_type: Optional[TokenType] = None
) -> Dict[str, Any]:
    """
    Verify signature + expiry and return the claims.

    Raises `AuthenticationError` for anything malformed, expired or of the
    wrong type — callers never need to know about PyJWT's exception tree.
    """
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(
            "Your session has expired. Please sign in again."
        ) from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError() from exc

    if expected_type is not None:
        # Tokens minted by the RAG chatbot predate the `typ` claim, so treat a
        # missing `typ` as an access token for SSO backwards compatibility.
        actual = claims.get("typ", TokenType.ACCESS.value)
        if actual != expected_type.value:
            raise AuthenticationError("This token cannot be used for that operation.")
    return claims


def generate_url_safe_token(length: int = 32) -> str:
    """Opaque token for invite links, password resets and idempotency keys."""
    return secrets.token_urlsafe(length)


# ── RBAC ───────────────────────────────────────────────────────────────────


def requires_role(*allowed_roles: str):
    """
    Decorator enforcing role-based access on a route handler.

    The handler must receive the authenticated principal as a `current_user`
    keyword argument (i.e. via `Depends(get_current_user)`), which is how all
    routes in `app/api/routes` are written:

        @router.post("/policies")
        @requires_role("admin", "hr")
        async def create_policy(..., current_user: User = Depends(get_current_user)):
            ...

    Route dependencies (`require_roles`) are the more idiomatic FastAPI form
    and are used for whole-router protection; this decorator exists for
    per-handler checks where the role list sits next to the handler body.
    """
    normalized = {role.lower() for role in allowed_roles}

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user = kwargs.get("current_user")
            if user is None:
                raise PermissionDeniedError(
                    "Endpoint is role-restricted but no authenticated user was resolved."
                )
            if not has_role(user, normalized):
                raise PermissionDeniedError(
                    f"This action requires one of: {', '.join(sorted(normalized))}."
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def has_role(user: Any, allowed_roles: Iterable[str]) -> bool:
    """True if `user.role` is in `allowed_roles`. Admins always pass."""
    raw = getattr(user, "role", None)
    role = getattr(raw, "value", raw)
    if role is None:
        return False
    role = str(role).lower()
    return role == "admin" or role in {r.lower() for r in allowed_roles}
