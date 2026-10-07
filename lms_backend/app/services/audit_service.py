"""
Audit trail writer.

Every mutating service calls this. Rows are appended inside the *same*
transaction as the change they describe, so a rolled-back approval can never
leave a phantom "approved" entry in the ledger.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.enums import ActionChannel, AuditAction

# Never copy secrets into the ledger.
_REDACTED_KEYS = {"password", "password_hash", "token", "secret"}


def _jsonable(value: Any) -> Any:
    """Make ORM column values JSON-serialisable without losing precision."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def snapshot(state: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if state is None:
        return None
    return {
        key: ("***" if key in _REDACTED_KEYS else _jsonable(value))
        for key, value in state.items()
    }


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: AuditAction,
    entity_type: str,
    entity_id: Optional[uuid.UUID] = None,
    actor_id: Optional[uuid.UUID] = None,
    actor_email: Optional[str] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    channel: ActionChannel = ActionChannel.WEB,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLog:
    """Append one immutable entry. Does not flush — the caller's commit does."""
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=snapshot(before),
        after_state=snapshot(after),
        channel=channel,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )
    session.add(entry)
    return entry
