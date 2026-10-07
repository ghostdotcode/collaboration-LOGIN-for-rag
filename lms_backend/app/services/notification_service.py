"""
Outbound notification dispatch.

The API never sends email inline — it hands work to Celery by *task name*.
Dispatching by name (rather than importing the task function) keeps the web
pods free of any Celery/RabbitMQ import, so the broker being down degrades
notifications instead of failing leave submissions.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.security import create_action_token

logger = logging.getLogger(__name__)

TASK_LEAVE_SUBMITTED = "app.tasks.email_worker.send_leave_submitted"
TASK_LEAVE_DECISION = "app.tasks.email_worker.send_leave_decision"
TASK_APPROVAL_REMINDER = "app.tasks.email_worker.send_approval_reminder"


def enqueue(task_name: str, payload: Dict[str, Any]) -> bool:
    """
    Best-effort enqueue. Returns False (and logs) instead of raising.

    A submitted leave request is durable in Postgres; an un-sent email is
    recoverable by a reminder sweep. Letting a broker hiccup roll back the
    request would be the worse failure.
    """
    try:
        from app.tasks.celery_app import celery_app
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("celery unavailable, dropping %s: %s", task_name, exc)
        return False

    try:
        celery_app.send_task(task_name, kwargs=payload)
        return True
    except Exception as exc:  # pragma: no cover - broker dependent
        logger.warning("failed to enqueue %s: %s", task_name, exc)
        return False


def build_action_links(
    *, request_id: uuid.UUID, approver_id: uuid.UUID, tenant_id: uuid.UUID
) -> Dict[str, str]:
    """
    One-click approve/reject URLs for email.

    Each link carries a short-lived token scoped to this request *and* this
    single action, so forwarding the approve mail can't be turned into a
    reject, and the link dies after `ACTION_TOKEN_EXPIRE_HOURS`.
    """
    base = f"{settings.APP_BASE_URL.rstrip('/')}{settings.API_V1_STR}/approvals/email-action"
    links = {}
    for action in ("approve", "reject"):
        token = create_action_token(
            request_id=request_id,
            approver_id=approver_id,
            tenant_id=tenant_id,
            action=action,
        )
        links[action] = f"{base}?token={token}"
    return links


def notify_request_submitted(
    *,
    tenant_id: uuid.UUID,
    leave_request_id: uuid.UUID,
    approver_id: Optional[uuid.UUID],
    approver_email: Optional[str],
    requester_name: str,
    leave_type_name: str,
    start_date: str,
    end_date: str,
    duration_days: str,
) -> bool:
    if not approver_email or approver_id is None:
        logger.info(
            "no approver resolved for request %s; skipping notification",
            leave_request_id,
        )
        return False

    links = build_action_links(
        request_id=leave_request_id, approver_id=approver_id, tenant_id=tenant_id
    )
    return enqueue(
        TASK_LEAVE_SUBMITTED,
        {
            "tenant_id": str(tenant_id),
            "leave_request_id": str(leave_request_id),
            "to_email": approver_email,
            "requester_name": requester_name,
            "leave_type_name": leave_type_name,
            "start_date": start_date,
            "end_date": end_date,
            "duration_days": duration_days,
            "approve_url": links["approve"],
            "reject_url": links["reject"],
        },
    )


def notify_decision(
    *,
    tenant_id: uuid.UUID,
    leave_request_id: uuid.UUID,
    to_email: str,
    decision: str,
    decided_by: str,
    comment: Optional[str],
    start_date: str,
    end_date: str,
) -> bool:
    return enqueue(
        TASK_LEAVE_DECISION,
        {
            "tenant_id": str(tenant_id),
            "leave_request_id": str(leave_request_id),
            "to_email": to_email,
            "decision": decision,
            "decided_by": decided_by,
            "comment": comment,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
