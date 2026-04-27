from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

WORKPLACE_TIMEZONE_NAME = "America/Toronto"
WORKPLACE_TIMEZONE = ZoneInfo(WORKPLACE_TIMEZONE_NAME)
GRACE_MINUTES = 10
AUTO_APPROVE_ADJUSTMENT_MINUTES = 60
MAX_SELF_SERVICE_ADJUSTMENT_MINUTES = 180
LONG_SHIFT_REVIEW_THRESHOLD_MINUTES = 540
ALLOWED_EARLY_START_ROLES = {"Store Manager", "Team Leader"}
CAPTAIN_ROLE = "Boat Captain"
CAPTAIN_STANDARD_START_MINUTES = 9 * 60
CAPTAIN_STANDARD_END_MINUTES = 17 * 60
CAPTAIN_CLOCK_IN_WINDOW_MINUTES = 60
CAPTAIN_CLOCK_OUT_LOCK_WINDOW_MINUTES = 30
CAPTAIN_CLOCK_OUT_ROUNDING_MINUTES = 15


@dataclass(frozen=True)
class BreakPolicyBand:
    label: str
    min_minutes: int
    max_minutes: int | None
    deduction_minutes: int
    requires_review: bool = False


BREAK_POLICY_BANDS = (
    BreakPolicyBand("Short shift", 0, 300, 0, False),
    BreakPolicyBand("Meal-break shift", 301, 509, 30, False),
    BreakPolicyBand("Standard shift", 510, 540, 60, False),
    BreakPolicyBand("Long shift", 541, None, 60, True),
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def pin_lookup_key(pin: str) -> str:
    secret = (
        os.getenv("TIME_CLOCK_PIN_PEPPER")
        or os.getenv("SESSION_SECRET")
        or "fobe-time-clock-dev-secret"
    )
    normalized = (pin or "").strip()
    return hashlib.sha256(f"{secret}:{normalized}".encode("utf-8")).hexdigest()


def utc_to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(WORKPLACE_TIMEZONE)


def local_now(now: datetime | None = None) -> datetime:
    return utc_to_local(now or now_utc()) or datetime.now(WORKPLACE_TIMEZONE)


def parse_time_string(value: str) -> int:
    raw = (value or "").strip()
    hh, mm = raw.split(":", 1)
    hours = int(hh)
    minutes = int(mm)
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        raise ValueError("Invalid clock time")
    return (hours * 60) + minutes


def format_minutes_as_clock(value: int | None) -> str | None:
    if value is None:
        return None
    total = max(0, int(value))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_local_time(value: datetime | None) -> str | None:
    local_value = utc_to_local(value)
    if local_value is None:
        return None
    return local_value.strftime("%H:%M")


def format_hours_from_minutes(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 60.0, 2)


def build_local_datetime(work_date: date, time_value: str) -> datetime:
    total_minutes = parse_time_string(time_value)
    local_value = datetime.combine(
        work_date,
        time(hour=total_minutes // 60, minute=total_minutes % 60),
        tzinfo=WORKPLACE_TIMEZONE,
    )
    return local_value


def local_datetime_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=WORKPLACE_TIMEZONE)
    return value.astimezone(timezone.utc)


def span_minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def minutes_since_midnight(value: datetime) -> int:
    local_value = utc_to_local(value) if value.tzinfo is not None else value
    if local_value is None:
        raise ValueError("datetime value is required")
    return (local_value.hour * 60) + local_value.minute


def set_minutes_since_midnight(value: datetime, total_minutes: int) -> datetime:
    normalized = max(0, min(23 * 60 + 59, int(total_minutes)))
    return datetime.combine(
        value.date(),
        time(hour=normalized // 60, minute=normalized % 60),
        tzinfo=value.tzinfo or WORKPLACE_TIMEZONE,
    )


def round_minutes_to_nearest_increment(total_minutes: int, increment: int) -> int:
    normalized = max(0, int(total_minutes))
    step = max(1, int(increment))
    quotient, remainder = divmod(normalized, step)
    if remainder * 2 >= step:
        quotient += 1
    return quotient * step


def captain_shift_is_full_day(start_minutes: int | None, end_minutes: int | None) -> bool:
    if start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
        return False
    return start_minutes <= CAPTAIN_STANDARD_START_MINUTES and end_minutes >= CAPTAIN_STANDARD_END_MINUTES


def normalize_captain_clock_in(value: datetime, role: str | None) -> datetime:
    if role != CAPTAIN_ROLE:
        return value
    minutes_value = minutes_since_midnight(value)
    if abs(minutes_value - CAPTAIN_STANDARD_START_MINUTES) <= CAPTAIN_CLOCK_IN_WINDOW_MINUTES:
        return set_minutes_since_midnight(value, CAPTAIN_STANDARD_START_MINUTES)
    return value


def normalize_captain_clock_out(value: datetime, role: str | None) -> datetime:
    if role != CAPTAIN_ROLE:
        return value
    minutes_value = minutes_since_midnight(value)
    if abs(minutes_value - CAPTAIN_STANDARD_END_MINUTES) <= CAPTAIN_CLOCK_OUT_LOCK_WINDOW_MINUTES:
        return set_minutes_since_midnight(value, CAPTAIN_STANDARD_END_MINUTES)
    if minutes_value < CAPTAIN_STANDARD_END_MINUTES:
        rounded_minutes = round_minutes_to_nearest_increment(minutes_value, CAPTAIN_CLOCK_OUT_ROUNDING_MINUTES)
        return set_minutes_since_midnight(value, rounded_minutes)
    return value


def break_policy_for_span(total_minutes: int) -> BreakPolicyBand:
    total = max(0, int(total_minutes))
    for band in BREAK_POLICY_BANDS:
        if total < band.min_minutes:
            continue
        if band.max_minutes is None or total <= band.max_minutes:
            return band
    return BREAK_POLICY_BANDS[-1]


def break_deduction_minutes_for_span(total_minutes: int) -> int:
    return break_policy_for_span(total_minutes).deduction_minutes


def payable_minutes_for_span(total_minutes: int) -> int:
    total = max(0, int(total_minutes))
    return max(0, total - break_deduction_minutes_for_span(total))


def scheduled_paid_minutes(start_time: str, end_time: str) -> int:
    return payable_minutes_for_span(parse_time_string(end_time) - parse_time_string(start_time))


def calculate_attendance_minutes(
    *,
    effective_clock_in_local: datetime,
    effective_clock_out_local: datetime,
    schedule_start_local: datetime | None = None,
    schedule_end_local: datetime | None = None,
    scheduled_paid_minutes_value: int | None = None,
    allow_scheduled_default: bool = True,
) -> tuple[int, int, bool]:
    worked_span = span_minutes(effective_clock_in_local, effective_clock_out_local)
    actual_deduction = break_deduction_minutes_for_span(worked_span)
    actual_payable = max(0, worked_span - actual_deduction)
    if (
        allow_scheduled_default
        and schedule_start_local is not None
        and schedule_end_local is not None
        and scheduled_paid_minutes_value is not None
    ):
        in_delta = abs(span_minutes(schedule_start_local, effective_clock_in_local))
        out_delta = abs(span_minutes(schedule_end_local, effective_clock_out_local))
        if effective_clock_in_local < schedule_start_local:
            in_delta = span_minutes(effective_clock_in_local, schedule_start_local)
        if effective_clock_out_local < schedule_end_local:
            out_delta = span_minutes(effective_clock_out_local, schedule_end_local)
        if in_delta <= GRACE_MINUTES and out_delta <= GRACE_MINUTES:
            scheduled_span = span_minutes(schedule_start_local, schedule_end_local)
            return (
                scheduled_paid_minutes_value,
                break_deduction_minutes_for_span(scheduled_span),
                True,
            )
    return (actual_payable, actual_deduction, False)
