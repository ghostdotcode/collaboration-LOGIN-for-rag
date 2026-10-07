"""
Transactional email delivery.

Tasks are enqueued by name from `services.notification_service`, so the API
process never imports this module (and therefore never needs Celery).
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from typing import Dict, Optional

from sqlalchemy import select, text

from app.core.config import settings
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Requests left un-actioned this long get a nudge.
REMINDER_AFTER_HOURS = 24


def _send(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """
    Deliver one message.

    Without SMTP configured (local dev, CI) the message is logged instead of
    sent, so the rest of the flow stays testable.
    """
    if not settings.SMTP_HOST:
        logger.info("SMTP not configured; would send %r to %s", subject, to_email)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.EMAIL_FROM
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
        smtp.starttls()
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)
    return True


def _layout(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f4f5f9;font-family:system-ui,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;padding:28px;">
    <h2 style="margin:0 0 16px;font-size:18px;color:#151829;">{escape(title)}</h2>
    {body_html}
    <p style="margin-top:24px;font-size:12px;color:#8892aa;">
      Sent by the Meritech Leave Management System.
    </p>
  </div>
</body></html>"""


def _button(url: str, label: str, color: str) -> str:
    return (
        f'<a href="{escape(url, quote=True)}" '
        f'style="display:inline-block;padding:11px 22px;margin-right:10px;'
        f"background:{color};color:#fff;text-decoration:none;border-radius:8px;"
        f'font-weight:600;font-size:14px;">{escape(label)}</a>'
    )


@celery_app.task(
    name="app.tasks.email_worker.send_leave_submitted",
    bind=True,
    autoretry_for=(smtplib.SMTPException, OSError),
    retry_backoff=True,
    max_retries=4,
)
def send_leave_submitted(
    self,
    *,
    tenant_id: str,
    leave_request_id: str,
    to_email: str,
    requester_name: str,
    leave_type_name: str,
    start_date: str,
    end_date: str,
    duration_days: str,
    approve_url: str,
    reject_url: str,
) -> bool:
    """
    Notify the approver, with one-click action buttons.

    The buttons carry single-action, expiring tokens, so a manager can
    approve from their phone without signing in — and a forwarded email
    cannot be replayed as the opposite decision.
    """
    body = f"""
      <p style="font-size:14px;color:#333;line-height:1.6;">
        <strong>{escape(requester_name)}</strong> has requested
        <strong>{escape(duration_days)} day(s)</strong> of
        {escape(leave_type_name)}.
      </p>
      <p style="font-size:14px;color:#333;">
        {escape(start_date)} &rarr; {escape(end_date)}
      </p>
      <div style="margin-top:20px;">
        {_button(approve_url, "Approve", "#16a34a")}
        {_button(reject_url, "Reject", "#dc2626")}
      </div>
    """
    text_body = (
        f"{requester_name} requested {duration_days} day(s) of {leave_type_name} "
        f"({start_date} to {end_date}).\n\nApprove: {approve_url}\nReject: {reject_url}"
    )
    return _send(
        to_email,
        f"Leave request from {requester_name}",
        _layout("Leave request awaiting your approval", body),
        text_body,
    )


@celery_app.task(
    name="app.tasks.email_worker.send_leave_decision",
    bind=True,
    autoretry_for=(smtplib.SMTPException, OSError),
    retry_backoff=True,
    max_retries=4,
)
def send_leave_decision(
    self,
    *,
    tenant_id: str,
    leave_request_id: str,
    to_email: str,
    decision: str,
    decided_by: str,
    comment: Optional[str],
    start_date: str,
    end_date: str,
) -> bool:
    """Tell the employee the outcome."""
    colour = "#16a34a" if decision == "approved" else "#dc2626"
    note = (
        f'<p style="font-size:13px;color:#555;">Comment: {escape(comment)}</p>'
        if comment
        else ""
    )
    body = f"""
      <p style="font-size:14px;color:#333;line-height:1.6;">
        Your leave request for <strong>{escape(start_date)} &rarr;
        {escape(end_date)}</strong> was
        <strong style="color:{colour};">{escape(decision)}</strong>
        by {escape(decided_by)}.
      </p>
      {note}
    """
    return _send(
        to_email,
        f"Your leave request was {decision}",
        _layout(f"Leave request {decision}", body),
        f"Your leave request ({start_date} to {end_date}) was {decision}. {comment or ''}",
    )


@celery_app.task(name="app.tasks.email_worker.send_approval_reminder")
def send_approval_reminder(
    *, to_email: str, pending_count: int, dashboard_url: str
) -> bool:
    body = f"""
      <p style="font-size:14px;color:#333;line-height:1.6;">
        You have <strong>{pending_count}</strong> leave request(s) waiting for
        your decision.
      </p>
      <div style="margin-top:18px;">{_button(dashboard_url, "Review requests", "#6366f1")}</div>
    """
    return _send(
        to_email,
        f"{pending_count} leave request(s) awaiting your approval",
        _layout("Pending approvals", body),
        f"You have {pending_count} leave request(s) awaiting approval: {dashboard_url}",
    )


@celery_app.task(name="app.tasks.email_worker.sweep_approval_reminders")
def sweep_approval_reminders() -> Dict[str, int]:
    """
    Daily nudge for approvals that have been sitting too long.

    Also the safety net for notification emails that failed to send when the
    request was first submitted — the request itself is always durable.
    """
    from app.db.session import get_sync_session_factory
    from app.models.approval_workflow import LeaveApproval
    from app.models.enums import ApprovalStatus, LeaveStatus
    from app.models.leave_request import LeaveRequest
    from app.models.tenant import Tenant
    from app.models.user import User

    cutoff = datetime.now(timezone.utc) - timedelta(hours=REMINDER_AFTER_HOURS)
    Session = get_sync_session_factory()
    reminded = 0

    with Session() as session:
        for tenant_id in list(session.scalars(select(Tenant.id))):
            session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            rows = session.execute(
                select(User.email, LeaveApproval.approver_id)
                .join(LeaveApproval, LeaveApproval.approver_id == User.id)
                .join(LeaveRequest, LeaveRequest.id == LeaveApproval.leave_request_id)
                .where(
                    LeaveApproval.tenant_id == tenant_id,
                    LeaveApproval.status == ApprovalStatus.PENDING,
                    LeaveApproval.level == LeaveRequest.current_level,
                    LeaveRequest.status == LeaveStatus.PENDING,
                    LeaveRequest.submitted_at < cutoff,
                )
            ).all()

            counts: Dict[str, int] = {}
            for email, _approver_id in rows:
                counts[email] = counts.get(email, 0) + 1

            for email, count in counts.items():
                send_approval_reminder.delay(
                    to_email=email,
                    pending_count=count,
                    dashboard_url=f"{settings.APP_BASE_URL.rstrip('/')}/frontend/lms/index.html",
                )
                reminded += 1

    logger.info("approval reminder sweep complete recipients=%s", reminded)
    return {"recipients": reminded}
