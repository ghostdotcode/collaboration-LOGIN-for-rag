"""
Unit tests for `app.services.leave_calculator`.

The spec demands 100% coverage here with explicit attention to leap years,
negative balances and weekend-overlap algorithms — these are the cases that
silently corrupt somebody's entitlement rather than raising an error.
"""

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import AccrualFrequency, DayPart
from app.services import leave_calculator as lc

MON_FRI = lc.WorkCalendar.build()
# Sun-Thu working week, as used across much of the Gulf.
SUN_THU = lc.WorkCalendar.build(working_days=[6, 0, 1, 2, 3])

D = Decimal


def dec(value: str) -> Decimal:
    return Decimal(value)


# ── Calendar primitives ────────────────────────────────────────────────────


def test_build_rejects_empty_working_week():
    with pytest.raises(ValueError):
        lc.WorkCalendar.build(working_days=[])


def test_build_rejects_out_of_range_weekday():
    with pytest.raises(ValueError):
        lc.WorkCalendar.build(working_days=[0, 7])


def test_weekend_detection_respects_local_working_week():
    saturday = date(2026, 9, 12)
    friday = date(2026, 9, 11)
    assert MON_FRI.is_weekend(saturday) is True
    assert MON_FRI.is_weekend(friday) is False
    # In a Sun-Thu week, Friday is the weekend and Sunday is a work day.
    assert SUN_THU.is_weekend(friday) is True
    assert SUN_THU.is_weekend(date(2026, 9, 13)) is False


def test_holiday_is_not_a_working_day():
    holiday = date(2026, 12, 25)
    cal = lc.WorkCalendar.build(holidays=[holiday])
    assert cal.is_holiday(holiday) is True
    assert cal.is_working_day(holiday) is False


def test_leap_year_helpers():
    assert lc.is_leap_year(2024) is True
    assert lc.is_leap_year(2025) is False
    assert lc.is_leap_year(2000) is True
    assert lc.is_leap_year(1900) is False
    assert lc.days_in_year(2024) == 366
    assert lc.days_in_year(2026) == 365
    assert lc.days_in_month(2024, 2) == 29
    assert lc.days_in_month(2026, 2) == 28


def test_quantize_and_half_day_rounding():
    assert lc.quantize(D("1.005")) == D("1.01")
    assert lc.round_to_half_day(D("0.26")) == D("0.50")
    assert lc.round_to_half_day(D("0.24")) == D("0.00")
    assert lc.round_to_half_day(D("1.75")) == D("2.00")


def test_safe_zoneinfo_falls_back_to_utc():
    assert lc.safe_zoneinfo("Not/AZone").key == "UTC"
    assert lc.safe_zoneinfo(None).key == "UTC"
    assert lc.safe_zoneinfo("Asia/Tokyo").key == "Asia/Tokyo"


def test_iter_dates_is_inclusive():
    days = list(lc.iter_dates(date(2026, 1, 1), date(2026, 1, 3)))
    assert days == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


# ── Working-day counting ───────────────────────────────────────────────────


def test_full_working_week_costs_five_days():
    # Mon 2026-09-07 .. Fri 2026-09-11
    assert count(date(2026, 9, 7), date(2026, 9, 11)) == dec("5.00")


def test_weekend_span_excludes_saturday_and_sunday():
    # Fri .. next Mon spans 4 calendar days but only 2 working days.
    breakdown = lc.working_day_breakdown(date(2026, 9, 11), date(2026, 9, 14), MON_FRI)
    assert breakdown.working_days == dec("2.00")
    assert breakdown.calendar_days == 4
    assert breakdown.weekend_days == 2


def test_weekend_only_request_costs_nothing():
    assert count(date(2026, 9, 12), date(2026, 9, 13)) == dec("0.00")


def test_holiday_inside_range_is_excluded_and_reported():
    cal = lc.WorkCalendar.build(holidays=[date(2026, 9, 9)])
    breakdown = lc.working_day_breakdown(date(2026, 9, 7), date(2026, 9, 11), cal)
    assert breakdown.working_days == dec("4.00")
    assert breakdown.holiday_days == 1
    assert breakdown.holidays_excluded == (date(2026, 9, 9),)


def test_single_day_half_leave_costs_half():
    day = date(2026, 9, 8)
    assert count(day, day, DayPart.FIRST_HALF, DayPart.FIRST_HALF) == dec("0.50")
    assert count(day, day, DayPart.SECOND_HALF, DayPart.SECOND_HALF) == dec("0.50")


def test_single_full_day_costs_one():
    day = date(2026, 9, 8)
    assert count(day, day) == dec("1.00")


def test_multi_day_starting_second_half():
    # Leaves after lunch Monday, back Wednesday morning: 1.5 days.
    result = count(
        date(2026, 9, 7), date(2026, 9, 8), DayPart.SECOND_HALF, DayPart.FULL_DAY
    )
    assert result == dec("1.50")


def test_multi_day_ending_first_half():
    result = count(
        date(2026, 9, 7), date(2026, 9, 9), DayPart.FULL_DAY, DayPart.FIRST_HALF
    )
    assert result == dec("2.50")


def test_both_boundaries_half():
    result = count(
        date(2026, 9, 7), date(2026, 9, 9), DayPart.SECOND_HALF, DayPart.FIRST_HALF
    )
    assert result == dec("2.00")


def test_half_day_on_a_weekend_boundary_is_not_a_credit():
    # Saturday half-day: the day was never chargeable, so no -0.5 discount.
    saturday = date(2026, 9, 12)
    assert count(saturday, saturday, DayPart.FIRST_HALF, DayPart.FIRST_HALF) == dec(
        "0.00"
    )
    # Range Sat..Tue starting "second half" of a Saturday still costs Mon+Tue.
    assert count(saturday, date(2026, 9, 15), DayPart.SECOND_HALF) == dec("2.00")


def test_half_day_on_a_holiday_boundary_is_not_a_credit():
    holiday = date(2026, 9, 11)  # a Friday
    cal = lc.WorkCalendar.build(holidays=[holiday])
    result = lc.count_working_days(
        date(2026, 9, 10),
        holiday,
        MON_FRI.__class__.build(holidays=[holiday]),
        DayPart.FULL_DAY,
        DayPart.FIRST_HALF,
    )
    assert result == dec("1.00")
    assert cal.is_working_day(holiday) is False


def test_gulf_working_week_counts_sunday_and_skips_friday():
    # Sun 2026-09-13 .. Thu 2026-09-17 is a full week in a Sun-Thu office.
    assert lc.count_working_days(date(2026, 9, 13), date(2026, 9, 17), SUN_THU) == dec(
        "5.00"
    )
    friday = date(2026, 9, 11)
    assert lc.count_working_days(friday, friday, SUN_THU) == dec("0.00")


def test_leap_day_is_a_normal_working_day():
    leap_day = date(2024, 2, 29)  # a Thursday
    assert lc.count_working_days(leap_day, leap_day, MON_FRI) == dec("1.00")


def test_end_before_start_is_rejected():
    with pytest.raises(ValueError):
        lc.count_working_days(date(2026, 9, 10), date(2026, 9, 9), MON_FRI)


def test_never_returns_negative_days():
    # A single weekend day marked half in both directions must clamp at zero.
    sunday = date(2026, 9, 13)
    assert count(sunday, sunday, DayPart.FIRST_HALF, DayPart.FIRST_HALF) == dec("0.00")


def test_hours_to_days_conversion():
    assert lc.hours_to_days(D("4"), MON_FRI) == dec("0.50")
    assert lc.hours_to_days(D("8"), MON_FRI) == dec("1.00")
    six_hour_day = lc.WorkCalendar.build(standard_hours_per_day=6)
    assert lc.hours_to_days(D("3"), six_hour_day) == dec("0.50")


def test_hours_to_days_rejects_zero_length_day():
    broken = lc.WorkCalendar(standard_hours_per_day=D("0"))
    with pytest.raises(ValueError):
        lc.hours_to_days(D("1"), broken)


def count(
    start: date,
    end: date,
    start_part: DayPart = DayPart.FULL_DAY,
    end_part: DayPart = DayPart.FULL_DAY,
) -> Decimal:
    return lc.count_working_days(start, end, MON_FRI, start_part, end_part)


# ── Leave year resolution ──────────────────────────────────────────────────


def test_calendar_leave_year():
    year = lc.resolve_leave_year(date(2026, 5, 20), fiscal_year_start_month=1)
    assert (year.key, year.start, year.end) == (
        2026,
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert year.total_days == 365
    assert year.contains(date(2026, 12, 31)) is True


def test_april_fiscal_year_maps_february_to_previous_year():
    year = lc.resolve_leave_year(date(2026, 2, 15), fiscal_year_start_month=4)
    assert year.key == 2025
    assert year.start == date(2025, 4, 1)
    assert year.end == date(2026, 3, 31)
    assert year.total_days == 365


def test_fiscal_year_spanning_a_leap_february_has_366_days():
    year = lc.resolve_leave_year(date(2024, 5, 1), fiscal_year_start_month=4)
    assert year.start == date(2024, 4, 1)
    assert year.end == date(2025, 3, 31)
    leap_year = lc.resolve_leave_year(date(2024, 1, 5), fiscal_year_start_month=1)
    assert leap_year.total_days == 366


def test_invalid_fiscal_month_is_rejected():
    with pytest.raises(ValueError):
        lc.resolve_leave_year(date(2026, 1, 1), fiscal_year_start_month=0)
    with pytest.raises(ValueError):
        lc.resolve_leave_year(date(2026, 1, 1), fiscal_year_start_month=13)


def test_rollover_expiry_date():
    year = lc.resolve_leave_year(date(2026, 6, 1))
    assert lc.rollover_expiry_date(year, 3) == date(2026, 3, 31)
    assert lc.rollover_expiry_date(year, 0) is None


# ── Accrual ────────────────────────────────────────────────────────────────

YEAR_2026 = lc.resolve_leave_year(date(2026, 6, 1))
MONTHLY_1_5 = lc.AccrualRule(
    frequency=AccrualFrequency.MONTHLY,
    rate_days=D("1.5"),
    annual_quota_days=D("18"),
)


def test_monthly_accrual_over_a_full_year_hits_the_quota():
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5, date(2020, 1, 1), date(2026, 12, 31), YEAR_2026
    )
    assert accrued == dec("18.00")


def test_monthly_accrual_mid_year_is_exact():
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5, date(2020, 1, 1), date(2026, 6, 30), YEAR_2026
    )
    assert accrued == dec("9.00")


def test_mid_month_joiner_is_prorated_by_actual_days():
    # Joined 16 Jan: 16 of January's 31 days => 1.5 * 16/31 = 0.774...
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5, date(2026, 1, 16), date(2026, 1, 31), YEAR_2026
    )
    assert accrued == dec("0.77")


def test_february_29_joiner_accrues_one_day_of_february():
    year_2024 = lc.resolve_leave_year(date(2024, 3, 1))
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5, date(2024, 2, 29), date(2024, 2, 29), year_2024
    )
    # Exactly 1 of February's 29 days.
    assert accrued == dec("0.05")


def test_february_29_joiner_full_year_accrual():
    year_2024 = lc.resolve_leave_year(date(2024, 3, 1))
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5, date(2024, 2, 29), date(2024, 12, 31), year_2024
    )
    # Mar-Dec = 10 full months (15.0) + 1/29 of February (0.0517).
    assert accrued == dec("15.05")


def test_probation_delays_accrual():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.MONTHLY,
        rate_days=D("1.5"),
        annual_quota_days=D("18"),
        accrual_starts_after_days=90,
    )
    # Joined 1 Jan, probation ends 1 April, so nothing before then.
    assert lc.accrued_days_as_of(
        rule, date(2026, 1, 1), date(2026, 3, 31), YEAR_2026
    ) == dec("0.00")
    assert lc.accrued_days_as_of(
        rule, date(2026, 1, 1), date(2026, 4, 30), YEAR_2026
    ) == dec("1.50")


def test_accrual_before_joining_is_zero():
    assert lc.accrued_days_as_of(
        MONTHLY_1_5, date(2026, 8, 1), date(2026, 7, 31), YEAR_2026
    ) == dec("0.00")


def test_non_prorating_policy_only_credits_whole_periods():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.MONTHLY,
        rate_days=D("1.5"),
        annual_quota_days=D("18"),
        prorate_on_joining=False,
    )
    # Partial January earns nothing...
    assert lc.accrued_days_as_of(
        rule, date(2026, 1, 16), date(2026, 1, 31), YEAR_2026
    ) == dec("0.00")
    # ...but February, served in full, earns the whole rate.
    assert lc.accrued_days_as_of(
        rule, date(2026, 1, 16), date(2026, 2, 28), YEAR_2026
    ) == dec("1.50")


def test_quarterly_accrual():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.QUARTERLY,
        rate_days=D("3"),
        annual_quota_days=D("12"),
    )
    assert lc.accrued_days_as_of(
        rule, date(2020, 1, 1), date(2026, 3, 31), YEAR_2026
    ) == dec("3.00")
    assert lc.accrued_days_as_of(
        rule, date(2020, 1, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("12.00")


def test_quarterly_quarters_anchor_to_fiscal_year():
    fiscal = lc.resolve_leave_year(date(2026, 5, 1), fiscal_year_start_month=4)
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.QUARTERLY,
        rate_days=D("3"),
        annual_quota_days=D("12"),
    )
    # First fiscal quarter is Apr-Jun, so end of June closes exactly one period.
    assert lc.accrued_days_as_of(
        rule, date(2020, 1, 1), date(2026, 6, 30), fiscal
    ) == dec("3.00")


def test_annual_frequency_prorates_across_the_year():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.ANNUALLY,
        rate_days=D("20"),
        annual_quota_days=D("20"),
    )
    assert lc.accrued_days_as_of(
        rule, date(2020, 1, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("20.00")


def test_fixed_quota_policy_prorates_for_a_mid_year_joiner():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.NONE,
        annual_quota_days=D("20"),
        prorate_on_joining=True,
    )
    # 1 Jul .. 31 Dec = 184 days of 365 => 20 * 184/365 = 10.08.
    assert lc.accrued_days_as_of(
        rule, date(2026, 7, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("10.08")


def test_fixed_quota_policy_without_proration_grants_everything():
    rule = lc.AccrualRule(
        frequency=AccrualFrequency.NONE,
        annual_quota_days=D("20"),
        prorate_on_joining=False,
    )
    assert lc.accrued_days_as_of(
        rule, date(2026, 7, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("20.00")


def test_zero_rate_accruing_policy_yields_nothing():
    rule = lc.AccrualRule(frequency=AccrualFrequency.MONTHLY, rate_days=D("0"))
    assert lc.accrued_days_as_of(
        rule, date(2020, 1, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("0.00")


def test_accrual_stops_at_exit_date():
    accrued = lc.accrued_days_as_of(
        MONTHLY_1_5,
        date(2020, 1, 1),
        date(2026, 12, 31),
        YEAR_2026,
        exit_date=date(2026, 3, 31),
    )
    assert accrued == dec("4.50")


def test_accrual_never_exceeds_annual_quota():
    generous = lc.AccrualRule(
        frequency=AccrualFrequency.MONTHLY,
        rate_days=D("5"),
        annual_quota_days=D("18"),
    )
    assert lc.accrued_days_as_of(
        generous, date(2020, 1, 1), date(2026, 12, 31), YEAR_2026
    ) == dec("18.00")


def test_accrual_rule_from_policy_adapter():
    class FakePolicy:
        accrual_frequency = AccrualFrequency.MONTHLY
        accrual_rate_days = D("1.25")
        annual_quota_days = D("15")
        prorate_on_joining = True
        accrual_starts_after_days = 0

    rule = lc.AccrualRule.from_policy(FakePolicy())
    assert rule.rate_days == D("1.25")
    assert rule.annual_quota_days == D("15")
    assert rule.frequency is AccrualFrequency.MONTHLY


def test_period_bounds_for_non_accruing_policy_is_the_whole_year():
    # Defensive branch: `accrued_days_as_of` short-circuits NONE before the
    # period loop, but the helper must still describe a sane period.
    start, end = lc._period_bounds(AccrualFrequency.NONE, date(2026, 6, 15), YEAR_2026)
    assert (start, end) == (YEAR_2026.start, YEAR_2026.end)


def test_accrual_start_date_respects_year_start_and_probation():
    rule = lc.AccrualRule(accrual_starts_after_days=30)
    # Joined years ago -> accrues from day one of this leave year.
    assert lc.accrual_start_date(rule, date(2019, 5, 5), YEAR_2026) == date(2026, 1, 1)
    # Joined this year -> accrues 30 days after joining.
    assert lc.accrual_start_date(rule, date(2026, 3, 1), YEAR_2026) == date(2026, 3, 31)


# ── Pro-rata quota ─────────────────────────────────────────────────────────


def test_prorated_quota_for_existing_employee_is_full():
    assert lc.prorated_annual_quota(D("20"), date(2019, 1, 1), YEAR_2026) == dec(
        "20.00"
    )


def test_prorated_quota_for_mid_year_joiner():
    assert lc.prorated_annual_quota(D("20"), date(2026, 7, 1), YEAR_2026) == dec(
        "10.08"
    )


def test_prorated_quota_for_someone_joining_next_year_is_zero():
    assert lc.prorated_annual_quota(D("20"), date(2027, 1, 1), YEAR_2026) == dec("0.00")


def test_prorated_quota_on_leap_day():
    year_2024 = lc.resolve_leave_year(date(2024, 6, 1))
    # 29 Feb .. 31 Dec 2024 = 307 days of 366.
    assert lc.prorated_annual_quota(D("24"), date(2024, 2, 29), year_2024) == dec(
        "20.13"
    )


# ── Rollover ───────────────────────────────────────────────────────────────


def test_rollover_under_the_cap_carries_everything():
    outcome = lc.compute_rollover(D("4"), D("5"))
    assert outcome.rolled_over == dec("4.00")
    assert outcome.lapsed == dec("0.00")


def test_rollover_over_the_cap_lapses_the_excess():
    outcome = lc.compute_rollover(D("9"), D("5"))
    assert outcome.rolled_over == dec("5.00")
    assert outcome.lapsed == dec("4.00")


def test_zero_cap_lapses_everything():
    outcome = lc.compute_rollover(D("7.5"), D("0"))
    assert outcome.rolled_over == dec("0.00")
    assert outcome.lapsed == dec("7.50")


def test_negative_balance_is_carried_forward_as_debt():
    outcome = lc.compute_rollover(D("-2.5"), D("5"))
    assert outcome.rolled_over == dec("-2.50")
    assert outcome.lapsed == dec("0.00")


def test_encashment_is_taken_before_rollover_cap():
    outcome = lc.compute_rollover(
        D("10"), D("5"), encashable_days=D("3"), allow_encashment=True
    )
    assert outcome.encashed == dec("3.00")
    assert outcome.rolled_over == dec("5.00")
    assert outcome.lapsed == dec("2.00")


def test_encashment_ignored_when_not_allowed():
    outcome = lc.compute_rollover(
        D("6"), D("5"), encashable_days=D("3"), allow_encashment=False
    )
    assert outcome.encashed == dec("0.00")
    assert outcome.rolled_over == dec("5.00")
    assert outcome.lapsed == dec("1.00")


def test_negative_rollover_cap_is_treated_as_zero():
    outcome = lc.compute_rollover(D("3"), D("-5"))
    assert outcome.rolled_over == dec("0.00")
    assert outcome.lapsed == dec("3.00")


# ── Overlap / clubbing ─────────────────────────────────────────────────────


def test_ranges_overlap_inclusive():
    assert lc.ranges_overlap(
        date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 5), date(2026, 1, 9)
    )
    assert not lc.ranges_overlap(
        date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 5), date(2026, 1, 9)
    )


def test_overlapping_dates_lists_shared_days():
    shared = lc.overlapping_dates(
        date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 4), date(2026, 1, 9)
    )
    assert shared == [date(2026, 1, 4), date(2026, 1, 5)]
    assert (
        lc.overlapping_dates(
            date(2026, 1, 1), date(2026, 1, 2), date(2026, 3, 1), date(2026, 3, 2)
        )
        == []
    )


def test_friday_and_monday_are_adjacent_across_the_weekend():
    friday = date(2026, 9, 11)
    monday = date(2026, 9, 14)
    assert lc.is_adjacent(friday, monday, MON_FRI) is True


def test_a_working_day_gap_breaks_adjacency():
    assert lc.is_adjacent(date(2026, 9, 7), date(2026, 9, 10), MON_FRI) is False


def test_overlapping_blocks_count_as_adjacent():
    assert lc.is_adjacent(date(2026, 9, 10), date(2026, 9, 9), MON_FRI) is True


def test_clubbing_rule_detection():
    assert lc.violates_clubbing_rule("SICK", ["ANNUAL"], ["annual"]) is True
    assert lc.violates_clubbing_rule("SICK", ["ANNUAL"], ["comp_off"]) is False
    assert lc.violates_clubbing_rule("SICK", [], ["annual"]) is False


# ── Timezone normalisation ─────────────────────────────────────────────────


def test_tokyo_day_maps_to_previous_evening_in_san_francisco():
    starts_at, ends_at = lc.local_window_to_utc(
        date(2026, 3, 3), date(2026, 3, 3), "Asia/Tokyo"
    )
    # 09:00 JST == 00:00 UTC; the SF manager sees Monday afternoon.
    assert starts_at == datetime(2026, 3, 3, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert ends_at == datetime(2026, 3, 3, 9, 0, tzinfo=ZoneInfo("UTC"))
    sf_local = starts_at.astimezone(ZoneInfo("America/Los_Angeles"))
    assert sf_local.date() == date(2026, 3, 2)


def test_first_half_day_ends_at_midday_local():
    _, ends_at = lc.local_window_to_utc(
        date(2026, 3, 3),
        date(2026, 3, 3),
        "UTC",
        DayPart.FIRST_HALF,
        DayPart.FIRST_HALF,
    )
    assert ends_at.time() == time(13, 0)


def test_multi_day_window_ending_first_half_closes_at_midday():
    starts_at, ends_at = lc.local_window_to_utc(
        date(2026, 3, 2), date(2026, 3, 4), "UTC", DayPart.FULL_DAY, DayPart.FIRST_HALF
    )
    assert starts_at.time() == time(9, 0)
    assert ends_at == datetime(2026, 3, 4, 13, 0, tzinfo=ZoneInfo("UTC"))


def test_second_half_day_starts_at_midday_local():
    starts_at, _ = lc.local_window_to_utc(
        date(2026, 3, 3),
        date(2026, 3, 3),
        "UTC",
        DayPart.SECOND_HALF,
        DayPart.SECOND_HALF,
    )
    assert starts_at.time() == time(13, 0)


def test_dst_transition_uses_the_correct_offset():
    # US DST begins 2026-03-08; 09:00 local is 13:00Z after the switch
    # and 14:00Z before it.
    before, _ = lc.local_window_to_utc(
        date(2026, 3, 6), date(2026, 3, 6), "America/New_York"
    )
    after, _ = lc.local_window_to_utc(
        date(2026, 3, 10), date(2026, 3, 10), "America/New_York"
    )
    assert before.hour == 14
    assert after.hour == 13


def test_unknown_timezone_falls_back_to_utc_instead_of_failing():
    starts_at, _ = lc.local_window_to_utc(
        date(2026, 3, 3), date(2026, 3, 3), "Mars/Olympus_Mons"
    )
    assert starts_at == datetime(2026, 3, 3, 9, 0, tzinfo=ZoneInfo("UTC"))


def test_local_today_uses_employee_timezone():
    # 23:30Z on 1 Jan is already 2 Jan in Tokyo.
    reference = datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("UTC"))
    assert lc.local_today("Asia/Tokyo", reference) == date(2026, 1, 2)
    assert lc.local_today("UTC", reference) == date(2026, 1, 1)


def test_local_today_accepts_naive_datetime_as_utc():
    naive = datetime(2026, 1, 1, 23, 30)
    assert lc.local_today("Asia/Tokyo", naive) == date(2026, 1, 2)


# ── Notice period ──────────────────────────────────────────────────────────


def test_notice_period_counts_only_working_days():
    # Submitted Monday for leave starting the following Monday: Tue-Fri = 4.
    assert notice(date(2026, 9, 7), date(2026, 9, 14)) == 4


def test_no_notice_for_same_day_or_backdated_requests():
    assert notice(date(2026, 9, 7), date(2026, 9, 7)) == 0
    assert notice(date(2026, 9, 7), date(2026, 9, 1)) == 0


def notice(submitted: date, start: date) -> int:
    return lc.notice_period_days(submitted, start, MON_FRI)
