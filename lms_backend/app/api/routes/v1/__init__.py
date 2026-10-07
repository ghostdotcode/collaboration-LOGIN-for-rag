"""v1 API router aggregation."""

from fastapi import APIRouter

from app.api.routes.v1 import (
    approvals,
    auth,
    balances,
    billing,
    dashboard,
    holidays,
    leaves,
    policies,
    users,
    workflows,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(leaves.router)
api_router.include_router(approvals.router)
api_router.include_router(balances.router)
api_router.include_router(users.router)
api_router.include_router(policies.router)
api_router.include_router(holidays.router)
api_router.include_router(workflows.router)
api_router.include_router(billing.router)

__all__ = ["api_router"]
