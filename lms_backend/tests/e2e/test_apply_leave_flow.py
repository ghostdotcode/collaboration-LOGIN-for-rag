"""
Browser end-to-end test of the apply-for-leave journey.

Requires a running stack and Playwright:

    uvicorn main:app --port 8000                 # chatbot + static frontend
    uvicorn app.main:app --port 8001             # LMS API
    playwright install chromium
    LMS_E2E_BASE_URL=http://localhost:8000 pytest tests/e2e -m e2e

Skips cleanly when either is absent, so `pytest` stays green on a laptop.
"""

from __future__ import annotations

import os

import pytest

BASE_URL = os.getenv("LMS_E2E_BASE_URL")

if not BASE_URL:
    pytest.skip("LMS_E2E_BASE_URL is not set", allow_module_level=True)

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e


def test_apply_for_leave_button_opens_the_lms(page: Page):
    """The chatbot header button must reach the LMS dashboard."""
    page.goto(f"{BASE_URL}/frontend/index.html")

    button = page.locator("#leave-btn")
    expect(button).to_be_visible()

    button.click()
    page.wait_for_url("**/frontend/lms/index.html*", timeout=10_000)
    expect(page.locator("#lms-app")).to_be_visible()


def test_dashboard_renders_balance_cards(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/index.html")
    # Either real balances or the explicit offline/empty state — never a
    # blank screen, which is the failure mode users actually report.
    expect(page.locator("#balance-grid, #dashboard-state").first).to_be_visible(
        timeout=10_000
    )


def test_apply_form_validates_date_order(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/apply.html")

    page.fill("#start-date", "2026-10-20")
    page.fill("#end-date", "2026-10-10")
    page.click("#submit-btn")

    expect(page.locator("#form-error")).to_be_visible()
    expect(page.locator("#form-error")).to_contain_text("end date", ignore_case=True)


def test_apply_form_is_usable_on_a_phone(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE_URL}/frontend/lms/apply.html")
    expect(page.locator("#apply-form")).to_be_visible()
    # No horizontal scroll: the layout must fit a 390px viewport.
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"layout overflows the viewport by {overflow}px"
