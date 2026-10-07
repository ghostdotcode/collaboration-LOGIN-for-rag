"""
Browser end-to-end tests for the manager and HR surfaces.

Same prerequisites as `test_apply_leave_flow.py`; skips cleanly without them.
These assert structure and client-side behaviour (tabs, month navigation,
bulk-selection state) rather than data, so they pass against an empty tenant.
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


def test_team_calendar_renders_a_six_week_grid(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/team.html")
    expect(page.locator("#cal-month")).not_to_be_empty(timeout=10_000)
    # Six rows of seven, always — a fixed grid stops the layout jumping
    # between months.
    expect(page.locator("#cal-grid .cal-cell")).to_have_count(42)


def test_month_navigation_changes_the_heading(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/team.html")
    expect(page.locator("#cal-month")).not_to_be_empty(timeout=10_000)
    first = page.locator("#cal-month").inner_text()

    page.click("#next-month")
    expect(page.locator("#cal-month")).not_to_have_text(first)

    page.click("#today-btn")
    expect(page.locator("#cal-month")).to_have_text(first)


def test_bulk_actions_stay_disabled_until_something_is_selected(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/team.html")
    expect(page.locator("#bulk-approve")).to_be_disabled()
    expect(page.locator("#bulk-reject")).to_be_disabled()


def test_admin_tabs_switch_panels(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/admin.html")
    expect(page.locator("#panel-types")).to_be_visible(timeout=10_000)
    expect(page.locator("#panel-yearend")).to_be_hidden()

    page.click('[data-panel="panel-yearend"]')
    expect(page.locator("#panel-yearend")).to_be_visible()
    expect(page.locator("#panel-types")).to_be_hidden()


def test_year_end_commit_is_locked_until_a_dry_run_has_happened(page: Page):
    """The destructive action must not be reachable in one click."""
    page.goto(f"{BASE_URL}/frontend/lms/admin.html")
    page.click('[data-panel="panel-yearend"]')
    expect(page.locator("#ye-commit")).to_be_disabled()


def test_workflow_builder_keeps_levels_consecutive(page: Page):
    page.goto(f"{BASE_URL}/frontend/lms/admin.html")
    page.click('[data-panel="panel-workflows"]')

    page.click("#wf-add-step")
    page.click("#wf-add-step")
    expect(page.locator("#wf-steps .step-row")).to_have_count(3)

    # Removing the middle level must renumber the rest, because the API
    # rejects a chain whose levels are not 1..n.
    page.locator("#wf-steps .step-row").nth(1).get_by_text("Remove").click()
    labels = page.locator("#wf-steps .level").all_inner_texts()
    assert labels == ["L1", "L2"]


def test_admin_layout_fits_a_phone(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE_URL}/frontend/lms/admin.html")
    expect(page.locator("#panel-types")).to_be_visible(timeout=10_000)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"layout overflows the viewport by {overflow}px"
