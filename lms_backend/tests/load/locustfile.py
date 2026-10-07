"""
Locust load profile for the LMS API.

Models the shape of real traffic rather than a uniform hammer: the Monday
9am pattern is overwhelmingly dashboard reads and estimate calls, with a
thin slice of writes. Submissions are the expensive path (row lock +
approval chain), so they are weighted to match reality instead of being
over-represented.

    locust -f tests/load/locustfile.py --host http://localhost:8001

Spec target: p99 < 200ms for reads at 10k concurrent users.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import date, timedelta

from locust import HttpUser, between, events, task

API = os.getenv("LMS_API_PREFIX", "/api/v1")
LOGIN_EMAIL = os.getenv("LOAD_TEST_EMAIL", "employee@acme.example")
LOGIN_PASSWORD = os.getenv("LOAD_TEST_PASSWORD", "correct-horse-battery")
TENANT_SLUG = os.getenv("LOAD_TEST_TENANT_SLUG", "acme")

# Fail the run if reads regress past the spec's budget.
P99_READ_BUDGET_MS = 200
MAX_FAIL_RATIO = 0.01


class EmployeeUser(HttpUser):
    """An employee checking balances and occasionally booking leave."""

    wait_time = between(1, 5)

    def on_start(self) -> None:
        self.leave_type_id = None
        response = self.client.post(
            f"{API}/auth/login",
            json={
                "email": LOGIN_EMAIL,
                "password": LOGIN_PASSWORD,
                "tenant_slug": TENANT_SLUG,
            },
            name="POST /auth/login",
        )
        if response.status_code != 200:
            self.environment.runner.quit()
            return
        token = response.json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

        types = self.client.get(f"{API}/leave-types", name="GET /leave-types")
        if types.status_code == 200 and types.json():
            self.leave_type_id = types.json()[0]["id"]

    @task(10)
    def dashboard(self) -> None:
        self.client.get(f"{API}/dashboard/me", name="GET /dashboard/me")

    @task(6)
    def balances(self) -> None:
        self.client.get(f"{API}/balances/me", name="GET /balances/me")

    @task(4)
    def my_requests(self) -> None:
        self.client.get(f"{API}/leaves?page=1&page_size=25", name="GET /leaves")

    @task(3)
    def estimate(self) -> None:
        if not self.leave_type_id:
            return
        start = date.today() + timedelta(days=random.randint(7, 120))
        self.client.post(
            f"{API}/leaves/estimate",
            json={
                "leave_type_id": self.leave_type_id,
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=random.randint(0, 3))).isoformat(),
            },
            name="POST /leaves/estimate",
        )

    @task(1)
    def submit(self) -> None:
        """
        The write path. Expect a healthy share of 409s under load — two users
        competing for the last day of balance *should* see a conflict, so
        those are recorded as successes rather than failures.
        """
        if not self.leave_type_id:
            return
        start = date.today() + timedelta(days=random.randint(30, 300))
        with self.client.post(
            f"{API}/leaves",
            json={
                "leave_type_id": self.leave_type_id,
                "start_date": start.isoformat(),
                "end_date": start.isoformat(),
                "reason": "Load test",
            },
            name="POST /leaves",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 409, 422):
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")


class ManagerUser(HttpUser):
    """A manager triaging their approval queue."""

    wait_time = between(2, 8)
    weight = 1

    def on_start(self) -> None:
        response = self.client.post(
            f"{API}/auth/login",
            json={
                "email": os.getenv("LOAD_TEST_MANAGER_EMAIL", "manager@acme.example"),
                "password": LOGIN_PASSWORD,
                "tenant_slug": TENANT_SLUG,
            },
            name="POST /auth/login (manager)",
        )
        if response.status_code == 200:
            self.client.headers.update(
                {"Authorization": f"Bearer {response.json()['access_token']}"}
            )

    @task(5)
    def pending_queue(self) -> None:
        self.client.get(f"{API}/approvals/pending", name="GET /approvals/pending")

    @task(2)
    def team_dashboard(self) -> None:
        self.client.get(f"{API}/dashboard/me", name="GET /dashboard/me (manager)")


@events.quitting.add_listener
def _enforce_latency_budget(environment, **_kwargs) -> None:
    """
    Turn the spec's p99 target into a build-failing assertion.

    Measured per endpoint rather than across the whole run: a fast dashboard
    would otherwise hide a slow approvals queue behind the aggregate, and the
    aggregate is not what a user experiences.
    """
    stats = environment.stats
    if stats.total.num_requests == 0:
        return

    breaches = []
    for (name, method), entry in stats.entries.items():
        # Writes take a row lock and build an approval chain; the 200ms budget
        # is a read-path target.
        if method != "GET" or entry.num_requests == 0:
            continue
        p99 = entry.get_response_time_percentile(0.99)
        if p99 and p99 > P99_READ_BUDGET_MS:
            breaches.append(f"{name}: p99 {p99:.0f}ms")

    if breaches:
        logging.error(
            "Read latency budget of %dms exceeded — %s",
            P99_READ_BUDGET_MS,
            "; ".join(breaches),
        )
        environment.process_exit_code = 1

    if stats.total.fail_ratio > MAX_FAIL_RATIO:
        logging.error(
            "Failure ratio %.2f%% exceeded budget", stats.total.fail_ratio * 100
        )
        environment.process_exit_code = 1
