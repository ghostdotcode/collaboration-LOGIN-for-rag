"""
Domain exceptions and the global handlers that translate them into
RFC-7807-ish JSON error payloads.

Services raise these; routes never build error responses by hand. Handlers
deliberately never echo back internal exception text in production.
"""

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

# Starlette renamed this constant (…_ENTITY -> …_CONTENT); referencing the old
# name emits a DeprecationWarning, so pin the code and stay version-agnostic.
HTTP_422_UNPROCESSABLE = 422


class LMSError(Exception):
    """Base class for all expected, domain-level failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "lms_error"
    message: str = "Request could not be processed."

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self, request_id: str) -> Dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class NotFoundError(LMSError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class AuthenticationError(LMSError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Invalid or expired authentication credentials."


class PermissionDeniedError(LMSError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class TenantInactiveError(LMSError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "tenant_inactive"
    message = "This workspace's subscription is not active."


class TenantSeatLimitError(LMSError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "seat_limit_reached"
    message = "Your subscription's employee limit has been reached."


class ValidationFailedError(LMSError):
    status_code = HTTP_422_UNPROCESSABLE
    code = "validation_failed"
    message = "The submitted data is invalid."


class PolicyViolationError(LMSError):
    status_code = status.HTTP_409_CONFLICT
    code = "policy_violation"
    message = "This request violates the configured leave policy."


class InsufficientBalanceError(LMSError):
    status_code = status.HTTP_409_CONFLICT
    code = "insufficient_balance"
    message = "You do not have enough leave balance for this request."


class OverlappingLeaveError(LMSError):
    status_code = status.HTTP_409_CONFLICT
    code = "overlapping_leave"
    message = "You already have a leave request covering these dates."


class InvalidStateTransitionError(LMSError):
    status_code = status.HTTP_409_CONFLICT
    code = "invalid_state_transition"
    message = "This request can no longer be modified."


class ConcurrencyError(LMSError):
    status_code = status.HTTP_409_CONFLICT
    code = "concurrent_modification"
    message = "Another update touched this record. Please retry."


class RateLimitExceededError(LMSError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limit_exceeded"
    message = "Too many requests. Please slow down."

    def __init__(self, message: Optional[str] = None, *, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ServiceUnavailableError(LMSError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "A downstream dependency is unavailable."


def _request_id(request: Request) -> str:
    existing = request.headers.get("X-Request-ID")
    return existing or str(uuid.uuid4())


def _cors_headers(request: Request, headers: Dict[str, str]) -> Dict[str, str]:
    """
    Add the CORS headers a browser needs to *read* an error response.

    Handlers registered for bare `Exception` run in Starlette's
    ServerErrorMiddleware, which sits outside CORSMiddleware — so without
    this, a 500 reaches the browser stripped of CORS headers and the fetch
    fails as an opaque network error, hiding the real message from the UI.
    """
    origin = request.headers.get("origin")
    if origin and origin in settings.BACKEND_CORS_ORIGINS:
        headers = {
            **headers,
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return headers


def register_exception_handlers(app: FastAPI) -> None:
    """Attach global handlers. Called once from `app.main`."""

    @app.exception_handler(LMSError)
    async def _handle_lms_error(request: Request, exc: LMSError) -> JSONResponse:
        rid = _request_id(request)
        logger.info(
            "domain_error code=%s path=%s request_id=%s",
            exc.code,
            request.url.path,
            rid,
        )
        headers = {"X-Request-ID": rid}
        if isinstance(exc, RateLimitExceededError):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(exc.to_payload(rid)),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = _request_id(request)
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content=jsonable_encoder(
                {
                    "error": {
                        "code": "validation_failed",
                        "message": "The submitted data is invalid.",
                        "details": {"fields": exc.errors()},
                        "request_id": rid,
                    }
                }
            ),
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        rid = _request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "http_error",
                    "message": exc.detail,
                    "details": {},
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(IntegrityError)
    async def _handle_integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        """A unique/check constraint fired — usually a lost race we can retry."""
        rid = _request_id(request)
        logger.warning("integrity_error path=%s request_id=%s", request.url.path, rid)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "constraint_violation",
                    "message": "This change conflicts with existing data.",
                    "details": {},
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(OperationalError)
    async def _handle_operational(
        request: Request, exc: OperationalError
    ) -> JSONResponse:
        rid = _request_id(request)
        logger.error(
            "db_operational_error path=%s request_id=%s", request.url.path, rid
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "database_unavailable",
                    "message": "The database is temporarily unavailable.",
                    "details": {},
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        rid = _request_id(request)
        logger.exception("unhandled_error path=%s request_id=%s", request.url.path, rid)
        message = (
            f"{type(exc).__name__}: {exc}"
            if not settings.is_production
            else "An internal error occurred."
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": message,
                    "details": {},
                    "request_id": rid,
                }
            },
            headers=_cors_headers(request, {"X-Request-ID": rid}),
        )
