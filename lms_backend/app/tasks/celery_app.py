"""
Celery application.

Queues are split (`accrual`, `email`, `default`) so a backlog of 500k
accrual tasks at midnight can't delay a manager's approval email, and so the
two workloads can be scaled independently in Kubernetes.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "lms",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks.accrual_worker", "app.tasks.email_worker"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Tasks are idempotent (accrual recomputes rather than increments), so
    # acking late is safe and means a killed worker's work gets redelivered.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Long tasks + fair dispatch: don't let one worker hoard the queue.
    worker_prefetch_multiplier=1,
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=86_400,
    task_default_queue="default",
    task_routes={
        "app.tasks.accrual_worker.*": {"queue": "accrual"},
        "app.tasks.email_worker.*": {"queue": "email"},
    },
    beat_schedule={
        # Belt-and-braces alongside the K8s CronJob: whichever fires, the
        # work is idempotent for a given `as_of` date.
        "nightly-accrual": {
            "task": "app.tasks.accrual_worker.run_nightly_accruals",
            "schedule": crontab(hour=0, minute=15),
        },
        "expire-stale-rollovers": {
            "task": "app.tasks.accrual_worker.expire_rollovers",
            "schedule": crontab(hour=1, minute=0),
        },
        "approval-reminders": {
            "task": "app.tasks.email_worker.sweep_approval_reminders",
            "schedule": crontab(hour=8, minute=0),
        },
    },
)

__all__ = ["celery_app"]
