"""Request metadata threaded through services for the audit trail."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.enums import ActionChannel


@dataclass(frozen=True)
class RequestContext:
    """
    Who/where an action came from.

    Carried explicitly rather than read from a context-local so that Celery
    workers and tests can construct one without faking an HTTP request.
    """

    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    channel: ActionChannel = ActionChannel.WEB

    @classmethod
    def system(cls) -> "RequestContext":
        return cls(channel=ActionChannel.SYSTEM)
