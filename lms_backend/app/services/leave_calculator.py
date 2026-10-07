"""
Leave mathematics: working days, pro-rata accrual, rollover and timezone
normalisation.

This module is deliberately **pure** — no database, no request context, no
clock reads except where a date is passed in. Every edge case from the spec
(leap years, Feb-29 joiners, mid-month starts, half days, non Mon-Fri work
weeks, cross-timezone day boundaries, fiscal leave years) is decided here so
it can be exhaustively unit tested without fixtures.

Money-like quantities are `Decimal`, never float: 0.1 + 0.2 != 0.3 is not an
acceptable answer when it is somebody's annual leave.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import FrozenSet, Iterable, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta

from app.models.enums import AccrualFrequency, DayPart

ZERO = Decimal("0.00")
HALF = Decimal("0.50")
ONE = Decimal("1.00")
CENTS = Decimal("0.01")

# Default office hours used to translate a local leave *date* into a UTC
# instant. Overridable per call so a tenant on a 07:00-15:00 shift is not
# forced into a 09:00-18:00 assumption.
DEFAULT_DAY_START = time(9, 0)
DEFAULT_DAY_END = time(18, 0)
DEFAULT_MIDDAY = time(13, 0)


def quantize(value: Decimal) -> Decimal:
    """Round to 2 decimal places, half-up (the way humans expect)."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def round_to_half_day(value: Decimal) -> Decimal:
    """Snap to the nearest 0.5 — used where policy forbids odd fractions."""
    return quantize(
        (Decimal(value) * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
    )


def is_leap_year(year: int) -> bool:
    return _calendar.isleap(year)


def days_in_year(year: int) -> int:
    return 366 if is_leap_year(year) else 365


def days_in_month(year: int, month: int) -> int:
    return _calendar.monthrange(year, month)[1]


def safe_zoneinfo(tz_name: Optional[str]) -> ZoneInfo:
    """Never let a bad/unknown timezone string take down a request."""
    try:
        return ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


# ── Working calendar ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkCalendar:
    """
    The employee's local working week plus their location's holidays.

    `working_days` uses `date.weekday()` numbering (0 = Monday), so a
    Sun-Thu working week in the Gulf is `{6, 0, 1, 2, 3}` and needs no
    special-casing anywhere else in the codebase.
    """

    working_days: FrozenSet[int] = frozenset({0, 1, 2, 3, 4})
    holidays: FrozenSet[date] = frozenset()
    standard_hours_per_day: Decimal = Decimal("8")

    @classmethod
    def build(
        cls,
        working_days: Optional[Iterable[int]] = None,
        holidays: Optional[Iterable[date]] = None,
        standard_hours_per_day: Decimal | int | str = 8,
    ) -> "WorkCalendar":
        # `None` means "use the default week"; an explicitly empty week is a
        # misconfiguration that would make every leave request free.
        source = (0, 1, 2, 3, 4) if working_days is None else working_days
        days = frozenset(int(d) for d in source)
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("working_days must be a non-empty set of ints in 0..6")
        return cls(
            working_days=days,
            holidays=frozenset(holidays or ()),
            standard_hours_per_day=Decimal(str(standard_hours_per_day)),
        )

    def is_weekend(self, day: date) -> bool:
        return day.weekday() not in self.working_days

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays

    def is_working_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)


def iter_dates(start: date, end: date) -> Iterator[date]:
    """Inclusive date iterator."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


@dataclass(frozen=True)
class WorkingDayBreakdown:
    """Explains a duration to the employee rather than just asserting it."""

    working_days: Decimal
    calendar_days: int
    weekend_days: int
    holiday_days: int
    holidays_excluded: Tuple[date, ...] = ()


def working_day_breakdown(
    start: date,
    end: date,
    work_calendar: WorkCalendar,
    start_day_part: DayPart = DayPart.FULL_DAY,
    end_day_part: DayPart = DayPart.FULL_DAY,
) -> WorkingDayBreakdown:
    """
    Count chargeable working days between two *local* dates, inclusive.

    Half-day rules:
      * Single day + FIRST_HALF/SECOND_HALF  -> 0.5
      * Multi-day starting SECOND_HALF       -> first day counts 0.5
      * Multi-day ending FIRST_HALF          -> last day counts 0.5
    Weekends and holidays never cost anything, including when they fall on
    the boundary day the employee nominated as a half day.
    """
    if end < start:
        raise ValueError("end date cannot be before start date")

    total = ZERO
    weekend_days = 0
    holiday_days = 0
    excluded: List[date] = []

    for day in iter_dates(start, end):
        if work_calendar.is_weekend(day):
            weekend_days += 1
            continue
        if work_calendar.is_holiday(day):
            holiday_days += 1
            excluded.append(day)
            continue
        total += ONE

    # Apply half-day adjustments only to boundary days that were actually
    # charged, so "second half of Saturday" doesn't create a -0.5 credit.
    if start == end:
        if start_day_part != DayPart.FULL_DAY and work_calendar.is_working_day(start):
            total -= HALF
    else:
        if start_day_part == DayPart.SECOND_HALF and work_calendar.is_working_day(
            start
        ):
            total -= HALF
        if end_day_part == DayPart.FIRST_HALF and work_calendar.is_working_day(end):
            total -= HALF

    return WorkingDayBreakdown(
        working_days=quantize(max(total, ZERO)),
        calendar_days=(end - start).days + 1,
        weekend_days=weekend_days,
        holiday_days=holiday_days,
        holidays_excluded=tuple(excluded),
    )


def count_working_days(
    start: date,
    end: date,
    work_calendar: WorkCalendar,
    start_day_part: DayPart = DayPart.FULL_DAY,
    end_day_part: DayPart = DayPart.FULL_DAY,
) -> Decimal:
    """Convenience wrapper returning just the chargeable day count."""
    return working_day_breakdown(
        start, end, work_calendar, start_day_part, end_day_part
    ).working_days


def hours_to_days(hours: Decimal, work_calendar: WorkCalendar) -> Decimal:
    """Convert an hourly leave request into fractional days."""
    if work_calendar.standard_hours_per_day <= 0:
        raise ValueError("standard_hours_per_day must be positive")
    return quantize(Decimal(hours) / work_calendar.standard_hours_per_day)


# ── Leave year (fiscal-aware) ──────────────────────────────────────────────


@dataclass(frozen=True)
class LeaveYear:
    """A tenant's leave year. `key` is what LeaveBalance.year stores."""

    key: int
    start: date
    end: date

    @property
    def total_days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def resolve_leave_year(on_date: date, fiscal_year_start_month: int = 1) -> LeaveYear:
    """
    Map a date to the leave year that contains it.

    A tenant on an April-March year sees 2026-02-15 as leave year 2025
    (2025-04-01 .. 2026-03-31), which is why balances are keyed by the
    *starting* calendar year rather than `date.year`.
    """
    if not 1 <= fiscal_year_start_month <= 12:
        raise ValueError("fiscal_year_start_month must be in 1..12")

    start_year = (
        on_date.year if on_date.month >= fiscal_year_start_month else on_date.year - 1
    )
    start = date(start_year, fiscal_year_start_month, 1)
    end = start + relativedelta(years=1) - timedelta(days=1)
    return LeaveYear(key=start_year, start=start, end=end)


def rollover_expiry_date(leave_year: LeaveYear, expiry_months: int) -> Optional[date]:
    """When carried-over days lapse. 0 months => they never lapse."""
    if expiry_months <= 0:
        return None
    return leave_year.start + relativedelta(months=expiry_months) - timedelta(days=1)


# ── Accrual ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccrualRule:
    """The subset of a LeavePolicy that accrual maths needs."""

    frequency: AccrualFrequency = AccrualFrequency.MONTHLY
    rate_days: Decimal = ZERO
    annual_quota_days: Decimal = ZERO
    prorate_on_joining: bool = True
    accrual_starts_after_days: int = 0

    @classmethod
    def from_policy(cls, policy) -> "AccrualRule":
        """Adapt an ORM LeavePolicy (or any duck-typed stand-in)."""
        return cls(
            frequency=policy.accrual_frequency,
            rate_days=Decimal(str(policy.accrual_rate_days or 0)),
            annual_quota_days=Decimal(str(policy.annual_quota_days or 0)),
            prorate_on_joining=bool(policy.prorate_on_joining),
            accrual_starts_after_days=int(policy.accrual_starts_after_days or 0),
        )


def accrual_start_date(
    rule: AccrualRule, joining_date: date, leave_year: LeaveYear
) -> date:
    """
    First date the employee earns anything.

    Probation delays accrual; someone who joined in a previous year starts
    accruing from day one of the current leave year.
    """
    eligible_from = joining_date + timedelta(days=rule.accrual_starts_after_days)
    return max(eligible_from, leave_year.start)


def _period_bounds(
    frequency: AccrualFrequency, day: date, leave_year: LeaveYear
) -> Tuple[date, date]:
    """Inclusive start/end of the accrual period containing `day`."""
    if frequency == AccrualFrequency.MONTHLY:
        start = day.replace(day=1)
        end = start + relativedelta(months=1) - timedelta(days=1)
    elif frequency == AccrualFrequency.QUARTERLY:
        # Quarters are anchored to the *leave* year, not the calendar year.
        months_in = (day.year - leave_year.start.year) * 12 + (
            day.month - leave_year.start.month
        )
        start = leave_year.start + relativedelta(months=(months_in // 3) * 3)
        end = start + relativedelta(months=3) - timedelta(days=1)
    elif frequency == AccrualFrequency.ANNUALLY:
        start, end = leave_year.start, leave_year.end
    else:  # AccrualFrequency.NONE
        start, end = leave_year.start, leave_year.end
    return start, end


def accrued_days_as_of(
    rule: AccrualRule,
    joining_date: date,
    as_of: date,
    leave_year: LeaveYear,
    exit_date: Optional[date] = None,
) -> Decimal:
    """
    Total days accrued from the start of the leave year up to `as_of`.

    Granular, daily-resolution maths: a period the employee was only present
    for part of (their joining month, their leaving month, or the month
    probation ended) is credited by the exact fraction of that period's days
    they were eligible for. This is what makes a Feb-29 joiner in a leap year
    come out correct without a special case — February simply has 29 days.

    `AccrualFrequency.NONE` means a fixed annual quota, pro-rated across the
    year for joiners/leavers when the policy says so.
    """
    window_start = accrual_start_date(rule, joining_date, leave_year)
    window_end = min(as_of, leave_year.end)
    if exit_date is not None:
        window_end = min(window_end, exit_date)
    if window_end < window_start:
        return ZERO

    if rule.frequency == AccrualFrequency.NONE:
        if not rule.prorate_on_joining:
            return quantize(rule.annual_quota_days)
        eligible_days = (window_end - window_start).days + 1
        fraction = Decimal(eligible_days) / Decimal(leave_year.total_days)
        return quantize(rule.annual_quota_days * fraction)

    if rule.rate_days <= 0:
        return ZERO

    total = Decimal(0)
    cursor = window_start
    while cursor <= window_end:
        period_start, period_end = _period_bounds(rule.frequency, cursor, leave_year)
        # Clip the period to both the leave year and the eligibility window.
        effective_start = max(period_start, window_start, leave_year.start)
        effective_end = min(period_end, window_end, leave_year.end)
        period_length = (period_end - period_start).days + 1
        covered = (effective_end - effective_start).days + 1

        if covered > 0:
            if rule.prorate_on_joining:
                total += rule.rate_days * Decimal(covered) / Decimal(period_length)
            elif covered == period_length:
                # No pro-rating: only whole periods earn anything.
                total += rule.rate_days

        cursor = period_end + timedelta(days=1)

    # An accruing policy still may not exceed its annual ceiling.
    if rule.annual_quota_days > 0:
        total = min(total, rule.annual_quota_days)
    return quantize(total)


def prorated_annual_quota(
    annual_quota_days: Decimal, joining_date: date, leave_year: LeaveYear
) -> Decimal:
    """
    Entitlement for someone present for only part of the leave year.

    Daily granularity, so joining on Feb 29 of a leap year or on the 31st of
    a 31-day month is handled by arithmetic rather than by branching.
    """
    if joining_date <= leave_year.start:
        return quantize(Decimal(annual_quota_days))
    if joining_date > leave_year.end:
        return ZERO
    remaining_days = (leave_year.end - joining_date).days + 1
    fraction = Decimal(remaining_days) / Decimal(leave_year.total_days)
    return quantize(Decimal(annual_quota_days) * fraction)


# ── Year-end rollover ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RolloverOutcome:
    rolled_over: Decimal
    lapsed: Decimal
    encashed: Decimal = ZERO


def compute_rollover(
    closing_balance: Decimal,
    max_rollover_days: Decimal,
    *,
    encashable_days: Decimal = ZERO,
    allow_encashment: bool = False,
) -> RolloverOutcome:
    """
    Split an unused closing balance into carried-forward, encashed and lapsed.

    A negative closing balance (the employee borrowed against next year)
    carries the debt forward rather than being silently forgiven.
    """
    closing = Decimal(closing_balance)
    if closing < 0:
        return RolloverOutcome(rolled_over=quantize(closing), lapsed=ZERO)

    encashed = ZERO
    if allow_encashment and encashable_days > 0:
        encashed = min(closing, Decimal(encashable_days))
        closing -= encashed

    rolled = min(closing, max(Decimal(max_rollover_days), ZERO))
    lapsed = closing - rolled
    return RolloverOutcome(
        rolled_over=quantize(rolled),
        lapsed=quantize(lapsed),
        encashed=quantize(encashed),
    )


# ── Overlap / conflict helpers ─────────────────────────────────────────────


def ranges_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    """Inclusive overlap test for two date ranges."""
    return a_start <= b_end and b_start <= a_end


def overlapping_dates(
    a_start: date, a_end: date, b_start: date, b_end: date
) -> List[date]:
    """The specific dates two ranges share (drives conflict warnings)."""
    if not ranges_overlap(a_start, a_end, b_start, b_end):
        return []
    return list(iter_dates(max(a_start, b_start), min(a_end, b_end)))


def is_adjacent(a_end: date, b_start: date, work_calendar: WorkCalendar) -> bool:
    """
    True if two leave blocks are effectively back-to-back.

    Only *working* days count as a gap, so Friday-then-Monday is adjacent —
    which is exactly the loophole the "sick leave cannot be clubbed with
    annual leave" rule exists to close.
    """
    if b_start <= a_end:
        return True
    gap = a_end + timedelta(days=1)
    while gap < b_start:
        if work_calendar.is_working_day(gap):
            return False
        gap += timedelta(days=1)
    return True


def violates_clubbing_rule(
    new_type_code: str,
    cannot_club_with: Sequence[str],
    adjacent_type_codes: Iterable[str],
) -> bool:
    """Whether the new request sits next to a leave type it may not touch."""
    forbidden = {code.lower() for code in cannot_club_with}
    if not forbidden:
        return False
    return any(code.lower() in forbidden for code in adjacent_type_codes)


# ── Timezone normalisation ─────────────────────────────────────────────────


def local_window_to_utc(
    start: date,
    end: date,
    timezone_name: str,
    start_day_part: DayPart = DayPart.FULL_DAY,
    end_day_part: DayPart = DayPart.FULL_DAY,
    *,
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
    midday: time = DEFAULT_MIDDAY,
) -> Tuple[datetime, datetime]:
    """
    Convert a locally-expressed leave window into UTC instants.

    The employee in Tokyo who books "Tuesday off" means 2026-03-03 09:00
    JST -> 18:00 JST, which is 2026-03-03 00:00Z -> 09:00Z, i.e. Monday
    evening for their manager in San Francisco. Storing both the local dates
    and these instants lets the UI show each person their own truth without
    the balance maths ever drifting by a day.
    """
    tz = safe_zoneinfo(timezone_name)

    local_start_time = midday if start_day_part == DayPart.SECOND_HALF else day_start
    if (
        start == end and start_day_part == DayPart.FIRST_HALF
    ) or end_day_part == DayPart.FIRST_HALF:
        local_end_time = midday
    else:
        local_end_time = day_end

    starts_at = datetime.combine(start, local_start_time, tzinfo=tz)
    ends_at = datetime.combine(end, local_end_time, tzinfo=tz)
    return (
        starts_at.astimezone(ZoneInfo("UTC")),
        ends_at.astimezone(ZoneInfo("UTC")),
    )


def local_today(timezone_name: str, now: Optional[datetime] = None) -> date:
    """'Today' from the employee's point of view, not the server's."""
    reference = now or datetime.now(tz=ZoneInfo("UTC"))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=ZoneInfo("UTC"))
    return reference.astimezone(safe_zoneinfo(timezone_name)).date()


def notice_period_days(
    submitted_on: date, leave_start: date, work_calendar: WorkCalendar
) -> int:
    """Working days of notice given — weekends don't count as notice."""
    if leave_start <= submitted_on:
        return 0
    return sum(
        1
        for day in iter_dates(
            submitted_on + timedelta(days=1), leave_start - timedelta(days=1)
        )
        if work_calendar.is_working_day(day)
    )
