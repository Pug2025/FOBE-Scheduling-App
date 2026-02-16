from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EmployeeRecord, ScheduleRun, SessionRecord, User
from app.security import hash_password, verify_password

app = FastAPI(title="FOBE Scheduler Prototype")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SESSION_COOKIE_NAME = "session_id"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
MANAGER_OR_ADMIN_ROLES = {"admin", "manager"}

UserRoleInput = Literal["admin", "manager", "view_only", "user"]
UserRole = Literal["admin", "manager", "view_only"]


@app.middleware("http")
async def disable_cache_for_auth_and_api(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class AuthPayload(BaseModel):
    email: str
    password: str


class UserCreatePayload(BaseModel):
    email: str
    temporary_password: str
    role: UserRoleInput = "view_only"


class UserPatchPayload(BaseModel):
    role: UserRoleInput | None = None
    temporary_password: str | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: int
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime

    @classmethod
    def from_orm_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            role=canonicalize_user_role(user.role),
            is_active=user.is_active,
            created_at=user.created_at,
        )


class ScheduleSavePayload(BaseModel):
    period_start: date
    weeks: int = Field(ge=1)
    label: str | None = None
    payload_json: dict[str, Any]
    result_json: dict[str, Any]


class ScheduleRunMetaOut(BaseModel):
    id: int
    created_at: datetime
    created_by_email: str
    period_start: date
    weeks: int
    label: str | None = None


class ScheduleRunOut(ScheduleRunMetaOut):
    payload_json: dict[str, Any]
    result_json: dict[str, Any]


class BootstrapStatusOut(BaseModel):
    enabled: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def canonicalize_user_role(raw_role: str) -> UserRole:
    if raw_role == "admin":
        return "admin"
    if raw_role == "manager":
        return "manager"
    if raw_role in {"view_only", "user"}:
        return "view_only"
    return "view_only"


def normalize_user_role_input(role: UserRoleInput) -> UserRole:
    if role == "user":
        return "view_only"
    return role


def ensure_password_strength(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 10 characters")


def request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        first_proto = forwarded_proto.split(",")[0].strip().lower()
        if first_proto:
            return first_proto == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def create_session(db: Session, user_id: int) -> str:
    while True:
        session_id = secrets.token_urlsafe(32)
        existing = db.get(SessionRecord, session_id)
        if existing is None:
            break
    db.add(
        SessionRecord(
            session_id=session_id,
            user_id=user_id,
            expires_at=utcnow() + timedelta(days=14),
        )
    )
    db.commit()
    return session_id


def delete_session_if_exists(db: Session, session_id: str) -> None:
    session = db.get(SessionRecord, session_id)
    if session is not None:
        db.delete(session)
        db.commit()


def get_session_user(db: Session, session_id: str | None) -> User | None:
    if not session_id:
        return None
    session = db.get(SessionRecord, session_id)
    if session is None:
        return None
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utcnow():
        db.delete(session)
        db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        db.delete(session)
        db.commit()
        return None
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_session_user(db, session_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if canonicalize_user_role(current_user.role) != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def get_manager_or_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if canonicalize_user_role(current_user.role) not in MANAGER_OR_ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager or admin access required")
    return current_user


def get_view_only_user(current_user: User = Depends(get_current_user)) -> User:
    if canonicalize_user_role(current_user.role) != "view_only":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="View-only access required")
    return current_user


def ensure_active_admin_remains(db: Session, target_user: User, patch: UserPatchPayload) -> None:
    next_role_raw = patch.role if patch.role is not None else target_user.role
    next_role = canonicalize_user_role(str(next_role_raw))
    next_is_active = patch.is_active if patch.is_active is not None else target_user.is_active
    if canonicalize_user_role(target_user.role) != "admin" or target_user.is_active is False:
        return
    if next_role == "admin" and next_is_active:
        return
    active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
    if active_admin_count <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")


def raise_user_write_error(exc: IntegrityError) -> None:
    message = str(getattr(exc, "orig", exc)).lower()
    if "ck_users_role" in message:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User role update failed because the database schema is out of date. Run migrations, then try again.",
        ) from exc
    if "users.email" in message and "unique" in message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists") from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to save user changes") from exc


class Period(BaseModel):
    start_date: date
    weeks: int = Field(default=2)


class SeasonRules(BaseModel):
    victoria_day: date
    june_30: date
    labour_day: date
    oct_31: date


class HoursRange(BaseModel):
    start: str
    end: str


class Hours(BaseModel):
    greystones: HoursRange
    beach_shop: HoursRange


class Coverage(BaseModel):
    greystones_weekday_staff: int
    greystones_weekend_staff: int
    beach_shop_staff: int


class LeadershipRules(BaseModel):
    min_team_leaders_every_open_day: int
    weekend_team_leaders_if_manager_off: int
    manager_two_consecutive_days_off_per_week: bool
    manager_min_weekends_per_month: int


Role = Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]
DayKey = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Employee(BaseModel):
    id: str
    name: str
    role: Role
    min_hours_per_week: int
    max_hours_per_week: int
    priority_tier: Literal["A", "B", "C"]
    student: bool = False
    availability: dict[str, list[str]]


class Unavailability(BaseModel):
    employee_id: str
    date: date
    reason: str = ""


class ExtraCoverageDay(BaseModel):
    date: date
    extra_people: int = 1


class AdHocBooking(BaseModel):
    employee_id: str
    date: date
    start: str
    end: str
    location: Literal["Greystones", "Beach Shop", "Boat"] = "Greystones"
    note: str = ""


class History(BaseModel):
    manager_weekends_worked_this_month: int = 0


class GenerateRequest(BaseModel):
    period: Period
    season_rules: SeasonRules
    hours: Hours
    coverage: Coverage
    leadership_rules: LeadershipRules
    employees: list[Employee]
    unavailability: list[Unavailability] = Field(default_factory=list)
    extra_coverage_days: list[ExtraCoverageDay] = Field(default_factory=list)  # deprecated: replaced by ad_hoc_bookings
    ad_hoc_bookings: list[AdHocBooking] = Field(default_factory=list)
    history: History = Field(default_factory=History)
    open_weekdays: list[DayKey] = Field(default_factory=lambda: DAY_KEYS.copy())
    week_start_day: DayKey = "sun"
    week_end_day: DayKey = "sat"
    reroll_token: int = Field(default=0, ge=0)
    schedule_beach_shop: bool = False
    shoulder_season: bool = False

    @model_validator(mode="after")
    def validate_week_boundaries(self) -> GenerateRequest:
        start_idx = DAY_KEYS.index(self.week_start_day)
        end_idx = DAY_KEYS.index(self.week_end_day)
        if (end_idx - start_idx) % 7 != 6:
            raise ValueError("week_start_day and week_end_day must define a full 7-day week boundary")
        if self.schedule_beach_shop and self.shoulder_season:
            raise ValueError("Shoulder season and Beach Shop scheduling cannot both be enabled")
        return self


class AssignmentOut(BaseModel):
    date: str
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    employee_name: str
    role: Role
    source: Literal["generated", "ad_hoc"] = "generated"


class ViewOnlyScheduleOut(ScheduleRunMetaOut):
    assignments: list[AssignmentOut]


class TotalsOut(BaseModel):
    week1_hours: float = 0
    week2_hours: float = 0
    week1_days: float = 0
    week2_days: float = 0
    weekend_days: int = 0
    locations: dict[str, int] = Field(default_factory=lambda: {"Greystones": 0, "Beach Shop": 0, "Boat": 0})


class ViolationOut(BaseModel):
    date: str
    type: Literal[
        "coverage_gap",
        "leader_gap",
        "manager_consecutive_days_off",
        "role_missing",
        "beach_shop_gap",
        "manager_days_rule",
        "hours_min_violation",
        "hours_max_violation",
        "ad_hoc_conflict",
    ]
    detail: str


class GenerateResponse(BaseModel):
    assignments: list[AssignmentOut]
    totals_by_employee: dict[str, TotalsOut]
    violations: list[ViolationOut]


_LAST_RESULT: GenerateResponse | None = None
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}


def _time_to_minutes(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def _hours_between(start: str, end: str) -> float:
    raw_hours = (_time_to_minutes(end) - _time_to_minutes(start)) / 60.0
    break_deduction = 1.0 if raw_hours >= 6 else 0.0
    return max(0.0, raw_hours - break_deduction)


def _format_hours(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _next_or_same_day(d: date, target_day: DayKey) -> date:
    days_until = (DAY_KEYS.index(target_day) - d.weekday()) % 7
    return d + timedelta(days=days_until)


def _next_sunday_after(d: date) -> date:
    days_until_next = (DAY_KEYS.index("sun") - d.weekday()) % 7
    if days_until_next == 0:
        days_until_next = 7
    return d + timedelta(days=days_until_next)


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _victoria_day(year: int) -> date:
    d = date(year, 5, 24)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def _season_rules_for_year(year: int) -> SeasonRules:
    return SeasonRules(
        victoria_day=_victoria_day(year),
        june_30=date(year, 6, 30),
        labour_day=_first_monday(year, 9),
        oct_31=date(year, 10, 31),
    )


def _normalized_season_rules(start_date: date, provided: SeasonRules) -> SeasonRules:
    years = {provided.victoria_day.year, provided.june_30.year, provided.labour_day.year, provided.oct_31.year}
    if years == {start_date.year}:
        return provided
    return _season_rules_for_year(start_date.year)


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _is_greystones_open(d: date, s: SeasonRules) -> bool:
    july_1 = date(d.year, 7, 1)
    if s.victoria_day <= d <= s.june_30:
        return d.weekday() >= 4
    if july_1 <= d <= s.labour_day:
        return True
    if (s.labour_day + timedelta(days=1)) <= d <= s.oct_31:
        return d.weekday() >= 4
    return False


def _is_beach_shop_open(d: date, s: SeasonRules) -> bool:
    july_1 = date(d.year, 7, 1)
    # Beach Shop runs on weekends year-round and can run on weekdays during peak summer.
    return d.weekday() >= 5 or (july_1 <= d <= s.labour_day)


def _week_index(day: date, start: date) -> int:
    return ((day - start).days // 7) + 1


def _week_start_for(day: date, schedule_start: date) -> date:
    return schedule_start + timedelta(days=((day - schedule_start).days // 7) * 7)


def _reroll_rank(employee_id: str, reroll_token: int) -> int:
    digest = hashlib.blake2b(f"{reroll_token}:{employee_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _choose_pair_for_manager_off(days: list[date], season: SeasonRules, extras: dict[date, int]) -> tuple[date, date]:
    pairs = []
    for i in range(len(days) - 1):
        d1, d2 = days[i], days[i + 1]
        score = int(_is_greystones_open(d1, season)) + int(_is_greystones_open(d2, season))
        score += extras.get(d1, 0) + extras.get(d2, 0)
        weekend_penalty = int(_is_weekend(d1)) + int(_is_weekend(d2))
        # Prefer weekday pairs for manager days off so weekends remain manager-covered by default.
        pairs.append((weekend_penalty, score, d1, d2))
    pairs.sort(key=lambda x: (x[0], x[1], x[2]))
    return (pairs[0][2], pairs[0][3]) if pairs else (days[0], days[0])


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _build_weekly_history_from_run(
    run: ScheduleRun,
) -> tuple[dict[tuple[date, str], float], dict[tuple[date, str], int], dict[tuple[date, str], set[date]]]:
    payload_json = run.payload_json or {}
    result_json = run.result_json or {}
    period = payload_json.get("period", {}) if isinstance(payload_json, dict) else {}
    raw_start = period.get("start_date") if isinstance(period, dict) else None
    run_start = _parse_iso_date(raw_start)
    if run_start is None:
        return ({}, {}, {})
    week_start_day = payload_json.get("week_start_day") if isinstance(payload_json, dict) else "sun"
    if week_start_day not in DAY_KEYS:
        week_start_day = "sun"
    run_start = _next_or_same_day(run_start, week_start_day)

    assignments = result_json.get("assignments") if isinstance(result_json, dict) else []
    if not isinstance(assignments, list):
        return ({}, {}, {})

    day_max_hours: dict[tuple[str, date], float] = defaultdict(float)
    leader_days: dict[tuple[date, str], set[date]] = defaultdict(set)
    worked_days: dict[tuple[date, str], set[date]] = defaultdict(set)
    for raw in assignments:
        if not isinstance(raw, dict):
            continue
        employee_id = raw.get("employee_id")
        shift_date = _parse_iso_date(raw.get("date"))
        if not employee_id or shift_date is None:
            continue
        raw_start_time = raw.get("start")
        raw_end_time = raw.get("end")
        if not isinstance(raw_start_time, str) or not isinstance(raw_end_time, str):
            continue
        shift_hours = _hours_between(raw_start_time, raw_end_time)
        day_key = (str(employee_id), shift_date)
        day_max_hours[day_key] = max(day_max_hours[day_key], shift_hours)
        week_start = _week_start_for(shift_date, run_start)
        worked_days[(week_start, str(employee_id))].add(shift_date)
        if raw.get("role") == "Team Leader" and raw.get("location") == "Greystones":
            leader_days[(week_start, str(employee_id))].add(shift_date)

    weekly_hours: dict[tuple[date, str], float] = defaultdict(float)
    for (employee_id, shift_day), hours in day_max_hours.items():
        week_start = _week_start_for(shift_day, run_start)
        weekly_hours[(week_start, employee_id)] += hours

    leader_day_counts = {(ws, emp_id): len(days) for (ws, emp_id), days in leader_days.items()}
    weekly_work_days = {(ws, emp_id): set(days) for (ws, emp_id), days in worked_days.items()}
    return (dict(weekly_hours), leader_day_counts, weekly_work_days)


def _load_generation_history_maps(
    db: Session,
    payload: GenerateRequest,
) -> tuple[dict[tuple[date, str], float], dict[tuple[date, str], int], dict[tuple[date, str], set[date]]]:
    schedule_start = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    runs = db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.period_start < schedule_start)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()

    # Use only the most recent finalized snapshot per week start.
    latest_week_stamp: dict[date, tuple[datetime, int]] = {}
    weekly_hours: dict[tuple[date, str], float] = {}
    leader_days: dict[tuple[date, str], int] = {}
    weekly_work_days: dict[tuple[date, str], set[date]] = {}
    for run in runs:
        run_weekly_hours, run_leader_days, run_weekly_work_days = _build_weekly_history_from_run(run)
        for key, hours in run_weekly_hours.items():
            week_start, _employee_id = key
            stamp = (run.created_at, run.id)
            if week_start in latest_week_stamp and stamp <= latest_week_stamp[week_start]:
                continue
            # Drop stale entries for the same week when a newer finalized run exists.
            for prior_key in [k for k in weekly_hours if k[0] == week_start]:
                weekly_hours.pop(prior_key, None)
            for prior_key in [k for k in leader_days if k[0] == week_start]:
                leader_days.pop(prior_key, None)
            for prior_key in [k for k in weekly_work_days if k[0] == week_start]:
                weekly_work_days.pop(prior_key, None)
            latest_week_stamp[week_start] = stamp
            for run_key, value in run_weekly_hours.items():
                if run_key[0] == week_start:
                    weekly_hours[run_key] = round(float(value), 2)
            for run_key, value in run_leader_days.items():
                if run_key[0] == week_start:
                    leader_days[run_key] = int(value)
            for run_key, value in run_weekly_work_days.items():
                if run_key[0] == week_start:
                    weekly_work_days[run_key] = set(value)
    return (weekly_hours, leader_days, weekly_work_days)


def _generate(
    payload: GenerateRequest,
    history_weekly_hours: dict[tuple[date, str], float] | None = None,
    history_weekly_leader_days: dict[tuple[date, str], int] | None = None,
    history_weekly_work_days: dict[tuple[date, str], set[date]] | None = None,
) -> GenerateResponse:
    start_date = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    season_rules = _normalized_season_rules(start_date, payload.season_rules)
    emp_map = {e.id: e for e in sorted(payload.employees, key=lambda x: x.id)}
    unavail = {(u.employee_id, u.date) for u in payload.unavailability}
    all_days = _daterange(start_date, payload.period.weeks * 7)
    week_starts = [start_date + timedelta(days=7 * i) for i in range(payload.period.weeks)]
    open_weekdays = set(payload.open_weekdays or DAY_KEYS)
    history_weekly_hours = history_weekly_hours or {}
    history_weekly_leader_days = history_weekly_leader_days or {}
    history_weekly_work_days = history_weekly_work_days or {}
    prior_week_start = start_date - timedelta(days=7)
    prior_week_worked_days: dict[str, set[date]] = {
        employee_id: set(history_weekly_work_days.get((prior_week_start, employee_id), set()))
        for employee_id in emp_map
    }

    def is_store_open(day: date) -> bool:
        return DAY_KEYS[day.weekday()] in open_weekdays

    open_days = [d for d in all_days if is_store_open(d)]
    open_day_index = {d: i for i, d in enumerate(open_days)}
    lead_ids = sorted([e.id for e in emp_map.values() if e.role == "Team Leader"])
    lead_pair = tuple(lead_ids[:2]) if len(lead_ids) == 2 else ()
    rotation_target_by_week: dict[date, str | None] = {}
    clerk_ids = [e.id for e in emp_map.values() if e.role == "Store Clerk"]
    clerk_lookback_hours: dict[str, float] = {}
    if not payload.shoulder_season:
        for clerk_id in clerk_ids:
            lookback_total = 0.0
            for weeks_ago in range(1, 5):
                prior_week = start_date - timedelta(days=7 * weeks_ago)
                lookback_total += history_weekly_hours.get((prior_week, clerk_id), 0.0)
            clerk_lookback_hours[clerk_id] = round(lookback_total, 2)
    ad_hoc_by_day: dict[date, list[AdHocBooking]] = defaultdict(list)
    for booking in payload.ad_hoc_bookings:
        if booking.date in all_days:
            ad_hoc_by_day[booking.date].append(booking)
    for day_bookings in ad_hoc_by_day.values():
        day_bookings.sort(key=lambda b: (b.start, b.employee_id, b.location))

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_assigned: dict[date, set[str]] = defaultdict(set)
    daily_hours_counted: dict[tuple[str, date], float] = defaultdict(float)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
    weekly_days: dict[tuple[str, int], int] = defaultdict(int)
    weekly_store_leader_days: dict[tuple[str, int], set[date]] = defaultdict(set)
    requested_days_off_by_week: dict[tuple[str, int], int] = defaultdict(int)
    for employee_id, day in unavail:
        if day in all_days and is_store_open(day):
            requested_days_off_by_week[(employee_id, _week_index(day, start_date))] += 1

    manager_ids = [e.id for e in emp_map.values() if e.role == "Store Manager"]
    manager_vacations_by_week: dict[tuple[str, date], int] = defaultdict(int)
    for manager_id in manager_ids:
        for day in all_days:
            if (manager_id, day) in unavail:
                week_start = _week_start_for(day, start_date)
                manager_vacations_by_week[(manager_id, week_start)] += 1

    forced_manager_off: set[date] = set()
    if payload.leadership_rules.manager_two_consecutive_days_off_per_week and manager_ids and not payload.shoulder_season:
        for ws in week_starts:
            week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
            if week_days:
                for manager_id in manager_ids:
                    if manager_vacations_by_week[(manager_id, ws)] > 0:
                        continue
                    week_open_days = [d for d in week_days if is_store_open(d)]
                    if len(week_open_days) < 2:
                        continue
                    a, b = _choose_pair_for_manager_off(week_open_days, season_rules, {})
                    forced_manager_off.update({a, b})

    def _rotation_target_from_counts(day_counts: dict[str, int]) -> str | None:
        if len(lead_pair) != 2:
            return None
        lead_a, lead_b = lead_pair
        a_days = day_counts.get(lead_a, 0)
        b_days = day_counts.get(lead_b, 0)
        if a_days > b_days:
            return lead_b
        if b_days > a_days:
            return lead_a
        return None

    def _lead_rotation_target_for_week(week_start: date) -> str | None:
        if payload.shoulder_season or len(lead_pair) != 2:
            return None
        if week_start in rotation_target_by_week:
            return rotation_target_by_week[week_start]
        if week_start == start_date:
            prior_week = week_start - timedelta(days=7)
            prior_counts = {
                lead_pair[0]: int(history_weekly_leader_days.get((prior_week, lead_pair[0]), 0)),
                lead_pair[1]: int(history_weekly_leader_days.get((prior_week, lead_pair[1]), 0)),
            }
            rotation_target_by_week[week_start] = _rotation_target_from_counts(prior_counts)
            return rotation_target_by_week[week_start]
        prior_week = week_start - timedelta(days=7)
        prior_idx = _week_index(prior_week, start_date)
        prior_counts = {
            lead_pair[0]: len(weekly_store_leader_days[(lead_pair[0], prior_idx)]),
            lead_pair[1]: len(weekly_store_leader_days[(lead_pair[1], prior_idx)]),
        }
        rotation_target_by_week[week_start] = _rotation_target_from_counts(prior_counts)
        return rotation_target_by_week[week_start]

    def prior_open_day_off_streak(employee_id: str, day: date) -> int:
        day_idx = open_day_index.get(day)
        if day_idx is None:
            return 0
        streak = 0
        for idx in range(day_idx - 1, -1, -1):
            prev_day = open_days[idx]
            if employee_id in daily_assigned[prev_day]:
                break
            streak += 1
        return streak

    def prior_consecutive_days_worked(employee_id: str, day: date) -> int:
        streak = 0
        cursor = day - timedelta(days=1)
        while True:
            if cursor >= start_date:
                if employee_id in daily_assigned[cursor]:
                    streak += 1
                    cursor -= timedelta(days=1)
                    continue
                break
            if cursor >= prior_week_start and cursor in prior_week_worked_days.get(employee_id, set()):
                streak += 1
                cursor -= timedelta(days=1)
                continue
            break
        return streak

    def eligible(day: date, role: Role, start: str, end: str, ignore_max: bool = False, allow_double_booking: bool = False) -> list[Employee]:
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        out: list[Employee] = []
        for e in emp_map.values():
            if e.role != role:
                continue
            if payload.shoulder_season and e.student and day.weekday() < 5:
                continue
            if (e.id, day) in unavail:
                continue
            if e.id not in daily_assigned[day] and prior_consecutive_days_worked(e.id, day) >= 5:
                continue
            if not allow_double_booking and e.id in daily_assigned[day]:
                continue
            if role == "Store Manager" and day in forced_manager_off:
                continue
            wk = _week_index(day, start_date)
            if role == "Store Manager" and (not payload.shoulder_season) and weekly_days[(e.id, wk)] >= 5:
                continue
            if not ignore_max and weekly_hours[(e.id, wk)] + _hours_between(start, end) > e.max_hours_per_week:
                continue
            windows = e.availability.get(DAY_KEYS[day.weekday()], [])
            fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
            if not fits:
                continue
            out.append(e)

        def work_pattern_penalty(employee_id: str) -> tuple[int, int]:
            yesterday = day - timedelta(days=1)
            two_days_ago = day - timedelta(days=2)
            worked_yesterday = yesterday in all_days and employee_id in daily_assigned[yesterday]
            worked_two_days_ago = two_days_ago in all_days and employee_id in daily_assigned[two_days_ago]
            starts_new_on_block = 0 if worked_yesterday else 1
            breaks_single_day_off = 1 if (not worked_yesterday and worked_two_days_ago) else 0
            return (starts_new_on_block, breaks_single_day_off)

        def off_streak_priority(employee_id: str) -> tuple[int, int]:
            streak = prior_open_day_off_streak(employee_id, day)
            if streak >= 3:
                return (0, -streak)
            if streak >= 2:
                return (1, 0)
            return (2, 0)

        def role_fairness_key(employee: Employee, week_idx: int) -> tuple[int, int, float]:
            if role == "Team Leader" and len(lead_pair) == 2 and not payload.shoulder_season:
                week_start = _week_start_for(day, start_date)
                preferred = _lead_rotation_target_for_week(week_start)
                if preferred in lead_pair:
                    other = lead_pair[1] if preferred == lead_pair[0] else lead_pair[0]
                    preferred_count = len(weekly_store_leader_days[(preferred, week_idx)])
                    other_count = len(weekly_store_leader_days[(other, week_idx)])
                    if employee.id == preferred:
                        new_diff = (preferred_count + 1) - other_count
                    else:
                        new_diff = preferred_count - (other_count + 1)
                    return (0, abs(new_diff - 1), 0.0 if employee.id == preferred else 1.0)
            if role == "Store Clerk":
                lookback_base = 0.0 if payload.shoulder_season else clerk_lookback_hours.get(employee.id, 0.0)
                return (
                    1,
                    PRIORITY_ORDER[employee.priority_tier],
                    round(lookback_base + weekly_hours[(employee.id, week_idx)], 2),
                )
            return (2, 0, 0.0)

        def max_hours_preference_key(employee: Employee, week_idx: int) -> tuple[int, float, int]:
            projected = weekly_hours[(employee.id, week_idx)] + _hours_between(start, end)
            overtime = max(0.0, round(projected - employee.max_hours_per_week, 2))
            if overtime == 0:
                # Normal priority ordering (A before B before C) when max-hours are respected.
                return (0, 0.0, PRIORITY_ORDER[employee.priority_tier])
            # If overtime is unavoidable, prefer lower-tier employees first to protect high-tier staff.
            overtime_priority = max(PRIORITY_ORDER.values()) - PRIORITY_ORDER[employee.priority_tier]
            return (1, overtime, overtime_priority)

        wk = _week_index(day, start_date)
        if role == "Store Clerk":
            out.sort(key=lambda e: (
                max_hours_preference_key(e, wk),
                role_fairness_key(e, wk),
                off_streak_priority(e.id),
                work_pattern_penalty(e.id),
                weekly_hours[(e.id, wk)],
                _reroll_rank(e.id, payload.reroll_token),
                e.name,
            ))
        else:
            out.sort(key=lambda e: (
                off_streak_priority(e.id),
                work_pattern_penalty(e.id),
                max_hours_preference_key(e, wk),
                role_fairness_key(e, wk),
                weekly_hours[(e.id, wk)],
                _reroll_rank(e.id, payload.reroll_token),
                e.name,
            ))
        return out

    def add_assignment(
        day: date,
        location: str,
        start: str,
        end: str,
        employee: Employee,
        role: Role,
        source: Literal["generated", "ad_hoc"] = "generated",
    ):
        assignments.append({
            "date": day,
            "location": location,
            "start": start,
            "end": end,
            "employee_id": employee.id,
            "employee_name": employee.name,
            "role": role,
            "source": source,
        })
        wk = _week_index(day, start_date)
        shift_hours = _hours_between(start, end)
        day_key = (employee.id, day)
        prior_counted = daily_hours_counted[day_key]
        new_counted = max(prior_counted, shift_hours)
        weekly_hours[(employee.id, wk)] += new_counted - prior_counted
        daily_hours_counted[day_key] = new_counted
        if employee.id not in daily_assigned[day]:
            weekly_days[(employee.id, wk)] += 1
        daily_assigned[day].add(employee.id)
        if role == "Team Leader" and location == "Greystones":
            weekly_store_leader_days[(employee.id, wk)].add(day)

    def assign_one(day: date, location: str, start: str, end: str, role: Role, needed: int, ignore_max: bool = False, allow_double_booking: bool = False):
        assigned_ids: set[str] = set()
        # Always try max-safe candidates first; exceed max only as fallback when explicitly allowed.
        for e in eligible(day, role, start, end, ignore_max=False, allow_double_booking=allow_double_booking):
            if len(assigned_ids) >= needed:
                break
            add_assignment(day, location, start, end, e, role)
            assigned_ids.add(e.id)
        if ignore_max and len(assigned_ids) < needed:
            for e in eligible(day, role, start, end, ignore_max=True, allow_double_booking=allow_double_booking):
                if len(assigned_ids) >= needed:
                    break
                if e.id in assigned_ids:
                    continue
                add_assignment(day, location, start, end, e, role)
                assigned_ids.add(e.id)

    def _is_floor_staff_assigned(employee_id: str, day: date) -> bool:
        return any(
            a["date"] == day
            and a["location"] == "Greystones"
            and a["employee_id"] == employee_id
            and a["role"] in {"Team Leader", "Store Clerk"}
            for a in assignments
        )

    def assign_beach_staff(day: date, start: str, end: str, needed: int) -> int:
        beach_assigned_ids = {
            a["employee_id"]
            for a in assignments
            if a["date"] == day and a["location"] == "Beach Shop"
        }
        floor_pulls = sum(1 for employee_id in beach_assigned_ids if _is_floor_staff_assigned(employee_id, day))
        max_floor_pulls = 1

        def assign_for_role(role: Role, role_needed: int) -> int:
            nonlocal floor_pulls
            assigned = 0
            for ignore_max in (False, True):
                for e in eligible(day, role, start, end, ignore_max=ignore_max, allow_double_booking=False):
                    if assigned >= role_needed:
                        break
                    if e.id in beach_assigned_ids:
                        continue
                    add_assignment(day, "Beach Shop", start, end, e, role)
                    beach_assigned_ids.add(e.id)
                    assigned += 1
                if assigned >= role_needed:
                    return assigned

            if floor_pulls >= max_floor_pulls:
                return assigned

            for ignore_max in (False, True):
                for e in eligible(day, role, start, end, ignore_max=ignore_max, allow_double_booking=True):
                    if assigned >= role_needed or floor_pulls >= max_floor_pulls:
                        break
                    if e.id in beach_assigned_ids:
                        continue
                    if not _is_floor_staff_assigned(e.id, day):
                        continue
                    add_assignment(day, "Beach Shop", start, end, e, role)
                    beach_assigned_ids.add(e.id)
                    floor_pulls += 1
                    assigned += 1
                if assigned >= role_needed or floor_pulls >= max_floor_pulls:
                    break
            return assigned

        remaining = max(0, needed)
        # Strong preference: fill Beach Shop slots with Store Clerks before Team Leaders.
        remaining -= assign_for_role("Store Clerk", remaining)
        if remaining > 0:
            remaining -= assign_for_role("Team Leader", remaining)
        return needed - remaining

    def _makeup_shift_for(role: Role) -> tuple[str, str, str]:
        greystones_shift = ("Greystones", payload.hours.greystones.start, payload.hours.greystones.end)
        if role == "Boat Captain":
            return ("Boat", payload.hours.greystones.start, payload.hours.greystones.end)
        if role in {"Store Manager", "Team Leader", "Store Clerk"}:
            return greystones_shift
        return greystones_shift

    def _can_add_makeup_shift(employee: Employee, day: date, start: str, end: str) -> bool:
        if (employee.id, day) in unavail:
            return False
        if employee.id in daily_assigned[day]:
            return False
        windows = employee.availability.get(DAY_KEYS[day.weekday()], [])
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
        if not fits:
            return False
        wk = _week_index(day, start_date)
        shift_hours = _hours_between(start, end)
        if weekly_hours[(employee.id, wk)] + shift_hours > employee.max_hours_per_week:
            return False
        if employee.role == "Store Manager" and day in forced_manager_off:
            return False
        if employee.role == "Store Manager" and (not payload.shoulder_season) and weekly_days[(employee.id, wk)] >= 5:
            return False
        return True

    def _preferred_makeup_days(role: Role, week_days: list[date]) -> list[date]:
        if role == "Team Leader":
            return sorted(
                week_days,
                key=lambda d: (
                    0 if d.weekday() == 5 else 1 if d.weekday() == 4 else 2,
                    d,
                ),
            )
        if role == "Store Clerk":
            return sorted(
                week_days,
                key=lambda d: (
                    0 if d.weekday() == 3 else 1 if d.weekday() == 4 else 2,
                    d,
                ),
            )
        return sorted(
            week_days,
            key=lambda d: (
                0 if d.weekday() == 3 else 1 if d.weekday() == 4 else 2,
                d,
            ),
        )

    def _location_role_compatible(role: Role, location: str) -> bool:
        if location == "Boat":
            return role == "Boat Captain"
        if location == "Beach Shop":
            return role in {"Store Clerk", "Team Leader"}
        if location == "Greystones":
            return role in {"Store Clerk", "Team Leader", "Store Manager"}
        return False

    def apply_ad_hoc_for_day(day: date):
        for booking in ad_hoc_by_day.get(day, []):
            employee = emp_map.get(booking.employee_id)
            if employee is None:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for unknown employee id {booking.employee_id} could not be scheduled",
                    )
                )
                continue
            if not is_store_open(day):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because the store is closed",
                    )
                )
                continue
            if not _location_role_compatible(employee.role, booking.location):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} is not compatible with {booking.location}",
                    )
                )
                continue
            if booking.location == "Beach Shop" and not _is_beach_shop_open(day, season_rules):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because Beach Shop is closed",
                    )
                )
                continue
            if (employee.id, day) in unavail:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} conflicts with requested time off",
                    )
                )
                continue
            if employee.id in daily_assigned[day]:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because they already have a shift that day",
                    )
                )
                continue
            try:
                smin = _time_to_minutes(booking.start)
                emin = _time_to_minutes(booking.end)
            except Exception:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} has an invalid time format",
                    )
                )
                continue
            if emin <= smin:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} has an invalid time range",
                    )
                )
                continue
            windows = employee.availability.get(DAY_KEYS[day.weekday()], [])
            fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
            if not fits:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} is outside availability",
                    )
                )
                continue
            if prior_consecutive_days_worked(employee.id, day) >= 5:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} would exceed 5 consecutive work days",
                    )
                )
                continue
            wk = _week_index(day, start_date)
            shift_hours = _hours_between(booking.start, booking.end)
            if weekly_hours[(employee.id, wk)] + shift_hours > employee.max_hours_per_week:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} would exceed weekly max hours",
                    )
                )
                continue
            add_assignment(day, booking.location, booking.start, booking.end, employee, employee.role, source="ad_hoc")

    for d in all_days:
        if is_store_open(d):
            g_start, g_end = payload.hours.greystones.start, payload.hours.greystones.end
            needed = payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff
            assign_one(d, "Greystones", g_start, g_end, "Store Manager", 1, ignore_max=payload.shoulder_season)
            manager_on = any(a for a in assignments if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Store Manager")
            if payload.shoulder_season and not manager_on:
                violations.append(ViolationOut(date=d.isoformat(), type="manager_days_rule", detail="Shoulder season requires a Store Manager on every open day"))
            manager_off = not manager_on
            manager_off_lead_target = max(2, payload.leadership_rules.weekend_team_leaders_if_manager_off)
            lead_need = max(payload.leadership_rules.min_team_leaders_every_open_day, manager_off_lead_target if manager_off else 1)
            # Manager-off lead rule should not be blocked by weekly max-hours limits.
            assign_one(d, "Greystones", g_start, g_end, "Team Leader", lead_need, ignore_max=manager_off)
            leaders_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Team Leader"
            ])
            if leaders_assigned < lead_need:
                detail = f"Greystones needed {lead_need} Team Leader(s)"
                if manager_off:
                    detail += " because no manager was scheduled"
                violations.append(ViolationOut(date=d.isoformat(), type="leader_gap", detail=detail))

            floor_roles = {"Team Leader", "Store Clerk"}
            floor_staff_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] in floor_roles
            ])
            assign_one(d, "Greystones", g_start, g_end, "Store Clerk", max(0, needed - floor_staff_assigned))
            floor_staff_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] in floor_roles
            ])
            if floor_staff_assigned < needed:
                violations.append(ViolationOut(date=d.isoformat(), type="coverage_gap", detail=f"Greystones needed {needed}"))

            captain = eligible(d, "Boat Captain", g_start, g_end, ignore_max=False)[:1]
            if not captain:
                # Captain must still be assigned when open, even if max hours must be exceeded.
                captain = eligible(d, "Boat Captain", g_start, g_end, ignore_max=True)[:1]
            if captain:
                add_assignment(d, "Boat", g_start, g_end, captain[0], "Boat Captain")
            else:
                violations.append(ViolationOut(date=d.isoformat(), type="role_missing", detail="Missing Boat Captain"))

        if payload.schedule_beach_shop and is_store_open(d) and _is_beach_shop_open(d, season_rules):
            b_start, b_end = payload.hours.beach_shop.start, payload.hours.beach_shop.end
            needed = 2
            added = assign_beach_staff(d, b_start, b_end, needed)
            if added < needed:
                violations.append(ViolationOut(date=d.isoformat(), type="beach_shop_gap", detail=f"Beach Shop needed {needed}"))

        # Ad hoc shifts are bolt-on additions and should not drive baseline staffing.
        apply_ad_hoc_for_day(d)

    # Meet weekly minimums even if that means exceeding baseline daily coverage.
    # Make-up day preference is role-specific (e.g., Team Leader Sat/Fri, Store Clerk Thu/Fri).
    if not payload.shoulder_season:
        for ws in week_starts:
            week_open_days = [
                ws + timedelta(days=i)
                for i in range(7)
                if (ws + timedelta(days=i)) in all_days and is_store_open(ws + timedelta(days=i))
            ]
            if not week_open_days:
                continue
            wk = _week_index(ws, start_date)
            for employee in emp_map.values():
                if weekly_hours[(employee.id, wk)] >= employee.min_hours_per_week:
                    continue
                if requested_days_off_by_week[(employee.id, wk)] > 0:
                    # Respect requested days off by avoiding forced make-up shifts.
                    continue
                makeup_days = _preferred_makeup_days(employee.role, week_open_days)
                location, shift_start, shift_end = _makeup_shift_for(employee.role)
                while weekly_hours[(employee.id, wk)] < employee.min_hours_per_week:
                    added = False
                    for day in makeup_days:
                        if not _can_add_makeup_shift(employee, day, shift_start, shift_end):
                            continue
                        add_assignment(day, location, shift_start, shift_end, employee, employee.role)
                        added = True
                        break
                    if not added:
                        break

    # Validate manager consecutive off rule.
    for ws in week_starts:
        week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
        for manager_id in manager_ids:
            if any(not is_store_open(d) for d in week_days):
                continue
            work = [manager_id in daily_assigned[d] for d in week_days]
            has_pair = any((not work[i]) and (not work[i + 1]) for i in range(len(work) - 1))
            if payload.leadership_rules.manager_two_consecutive_days_off_per_week and (not payload.shoulder_season) and not has_pair:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_consecutive_days_off", detail=f"Manager {emp_map[manager_id].name} lacks consecutive days off"))
            requested_days_off = sum(1 for d in week_days if (manager_id, d) in unavail)
            target_days = max(0, (len(week_days) - requested_days_off) if payload.shoulder_season else min(5, len(week_days) - requested_days_off))
            actual_days = sum(work)
            if actual_days < target_days:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_days_rule", detail=f"Manager {emp_map[manager_id].name} scheduled {actual_days} day(s), minimum is {target_days}"))

    for ws in week_starts:
        wk = _week_index(ws, start_date)
        for e in emp_map.values():
            scheduled_hours = round(weekly_hours[(e.id, wk)], 2)
            if (not payload.shoulder_season) and scheduled_hours < e.min_hours_per_week and requested_days_off_by_week[(e.id, wk)] == 0:
                violations.append(
                    ViolationOut(
                        date=ws.isoformat(),
                        type="hours_min_violation",
                        detail=f"{e.name} scheduled {_format_hours(scheduled_hours)}h, minimum is {e.min_hours_per_week}h",
                    )
                )
            if scheduled_hours > e.max_hours_per_week:
                violations.append(
                    ViolationOut(
                        date=ws.isoformat(),
                        type="hours_max_violation",
                        detail=f"{e.name} scheduled {_format_hours(scheduled_hours)}h, maximum is {e.max_hours_per_week}h",
                    )
                )

    totals: dict[str, TotalsOut] = {e.id: TotalsOut() for e in emp_map.values()}
    for e in emp_map.values():
        totals[e.id].week1_hours = round(weekly_hours[(e.id, 1)], 2)
        totals[e.id].week2_hours = round(weekly_hours[(e.id, 2)], 2)

    daily_presence_by_employee: dict[tuple[str, date], dict[str, bool]] = defaultdict(lambda: {"non_beach": False, "beach": False})
    weekend_days_by_employee: dict[str, set[date]] = defaultdict(set)
    for a in assignments:
        day_presence = daily_presence_by_employee[(a["employee_id"], a["date"])]
        if a["location"] == "Beach Shop":
            day_presence["beach"] = True
        else:
            day_presence["non_beach"] = True

    for (employee_id, work_day), flags in daily_presence_by_employee.items():
        wk = _week_index(work_day, start_date)
        day_credit = 1.0 if flags["non_beach"] else 0.5
        if wk == 1:
            totals[employee_id].week1_days += day_credit
        elif wk == 2:
            totals[employee_id].week2_days += day_credit
        if _is_weekend(work_day):
            weekend_days_by_employee[employee_id].add(work_day)

    for a in assignments:
        totals[a["employee_id"]].locations[a["location"]] += 1

    for e in emp_map.values():
        totals[e.id].week1_days = round(totals[e.id].week1_days, 2)
        totals[e.id].week2_days = round(totals[e.id].week2_days, 2)
        totals[e.id].weekend_days = len(weekend_days_by_employee[e.id])

    out_assignments = [
        AssignmentOut(
            date=a["date"].isoformat(),
            location=a["location"],
            start=a["start"],
            end=a["end"],
            employee_id=a["employee_id"],
            employee_name=a["employee_name"],
            role=a["role"],
            source=a.get("source", "generated"),
        )
        for a in sorted(assignments, key=lambda x: (x["date"], x["location"], x["employee_name"]))
    ]
    return GenerateResponse(assignments=out_assignments, totals_by_employee=totals, violations=sorted(violations, key=lambda v: (v.date, v.type, v.detail)))


def _sample_payload_dict() -> dict:
    today = date.today()
    default_start = _next_sunday_after(today)
    default_rules = _season_rules_for_year(default_start.year)
    return {
        "period": {"start_date": default_start.isoformat(), "weeks": 2},
        "season_rules": {
            "victoria_day": default_rules.victoria_day.isoformat(),
            "june_30": default_rules.june_30.isoformat(),
            "labour_day": default_rules.labour_day.isoformat(),
            "oct_31": default_rules.oct_31.isoformat(),
        },
        "hours": {"greystones": {"start": "08:30", "end": "17:30"}, "beach_shop": {"start": "12:00", "end": "16:00"}},
        "coverage": {"greystones_weekday_staff": 3, "greystones_weekend_staff": 4, "beach_shop_staff": 2},
        "leadership_rules": {"min_team_leaders_every_open_day": 1, "weekend_team_leaders_if_manager_off": 2, "manager_two_consecutive_days_off_per_week": True, "manager_min_weekends_per_month": 2},
        "employees": [
            {"id": "manager_mia", "name": "Manager Mia", "role": "Store Manager", "min_hours_per_week": 24, "max_hours_per_week": 40, "priority_tier": "A", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "taylor", "name": "Taylor", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "A", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "sam", "name": "Sam", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "casey", "name": "Casey", "role": "Boat Captain", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "jordan", "name": "Jordan", "role": "Store Clerk", "min_hours_per_week": 16, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
        ],
        "unavailability": [],
        "ad_hoc_bookings": [],
        "history": {"manager_weekends_worked_this_month": 0},
        "open_weekdays": DAY_KEYS,
        "week_start_day": "sun",
        "week_end_day": "sat",
        "reroll_token": 0,
        "schedule_beach_shop": False,
        "shoulder_season": False,
    }

def serialize_employee_record(record: EmployeeRecord) -> Employee:
    return Employee(
        id=record.employee_id,
        name=record.name,
        role=record.role,
        min_hours_per_week=record.min_hours_per_week,
        max_hours_per_week=record.max_hours_per_week,
        priority_tier=record.priority_tier,
        student=record.student,
        availability=record.availability,
    )


def serialize_roster(records: list[EmployeeRecord]) -> list[Employee]:
    return [serialize_employee_record(record) for record in records]


def serialize_schedule_meta(run: ScheduleRun, created_by_email: str) -> ScheduleRunMetaOut:
    return ScheduleRunMetaOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
    )


def serialize_schedule_out(run: ScheduleRun, created_by_email: str) -> ScheduleRunOut:
    return ScheduleRunOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
        payload_json=run.payload_json,
        result_json=run.result_json,
    )


def extract_assignments_from_result_json(result_json: Any) -> list[AssignmentOut]:
    if not isinstance(result_json, dict):
        return []
    raw_assignments = result_json.get("assignments")
    if not isinstance(raw_assignments, list):
        return []
    parsed: list[AssignmentOut] = []
    for raw_assignment in raw_assignments:
        try:
            parsed.append(AssignmentOut.model_validate(raw_assignment))
        except Exception:
            continue
    return parsed


def serialize_view_only_schedule(run: ScheduleRun, created_by_email: str) -> ViewOnlyScheduleOut:
    return ViewOnlyScheduleOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
        assignments=extract_assignments_from_result_json(run.result_json),
    )


def ensure_valid_email(email: str) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email is required")
    return normalized


@app.post("/auth/bootstrap", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def auth_bootstrap(
    payload: AuthPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
) -> UserOut:
    configured_token = os.getenv("BOOTSTRAP_TOKEN", "")
    if not configured_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Bootstrap token is not configured")
    if bootstrap_token != configured_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid bootstrap token")
    existing_users = db.scalar(select(func.count(User.id))) or 0
    if existing_users > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap is only allowed before the first user exists")
    email = ensure_valid_email(payload.email)
    ensure_password_strength(payload.password)
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    session_id = create_session(db, user.id)
    set_session_cookie(response, request, session_id)
    return UserOut.from_orm_user(user)


@app.get("/auth/bootstrap/status", response_model=BootstrapStatusOut)
def auth_bootstrap_status(db: Session = Depends(get_db)) -> BootstrapStatusOut:
    configured_token = bool(os.getenv("BOOTSTRAP_TOKEN", ""))
    if not configured_token:
        return BootstrapStatusOut(enabled=False)
    existing_users = db.scalar(select(func.count(User.id))) or 0
    return BootstrapStatusOut(enabled=existing_users == 0)


@app.post("/auth/login", response_model=UserOut)
def auth_login(
    payload: AuthPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> UserOut:
    email = ensure_valid_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
    session_id = create_session(db, user.id)
    set_session_cookie(response, request, session_id)
    return UserOut.from_orm_user(user)


@app.post("/auth/logout")
def auth_logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session_if_exists(db, session_id)
    clear_session_cookie(response, request)
    return {"ok": True}


@app.get("/auth/me", response_model=UserOut)
def auth_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_orm_user(current_user)


@app.get("/api/employees", response_model=list[Employee])
def get_employees(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[Employee]:
    records = db.scalars(select(EmployeeRecord).order_by(EmployeeRecord.sort_order, EmployeeRecord.id)).all()
    return serialize_roster(list(records))


@app.put("/api/employees", response_model=list[Employee])
def put_employees(
    employees: list[Employee] = Body(...),
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[Employee]:
    employee_ids = [employee.id for employee in employees]
    if len(employee_ids) != len(set(employee_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee ids must be unique")
    db.execute(delete(EmployeeRecord))
    for index, employee in enumerate(employees):
        db.add(
            EmployeeRecord(
                employee_id=employee.id,
                name=employee.name,
                role=employee.role,
                min_hours_per_week=employee.min_hours_per_week,
                max_hours_per_week=employee.max_hours_per_week,
                priority_tier=employee.priority_tier,
                student=employee.student,
                availability=employee.availability,
                sort_order=index,
            )
        )
    db.commit()
    return employees


@app.get("/api/admin/users", response_model=list[UserOut])
def admin_list_users(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.created_at.asc(), User.id.asc())).all()
    return [UserOut.from_orm_user(user) for user in users]


@app.post("/api/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    payload: UserCreatePayload,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> UserOut:
    email = ensure_valid_email(payload.email)
    ensure_password_strength(payload.temporary_password)
    user = User(
        email=email,
        password_hash=hash_password(payload.temporary_password),
        role=normalize_user_role_input(payload.role),
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_user_write_error(exc)
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.patch("/api/admin/users/{user_id}", response_model=UserOut)
def admin_patch_user(
    user_id: int,
    payload: UserPatchPayload,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if payload.role is None and payload.temporary_password is None and payload.is_active is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updates were provided")
    if user.id == current_admin.id:
        if payload.role is not None and normalize_user_role_input(payload.role) != "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role while signed in")
        if payload.is_active is False:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot disable your own account while signed in")
    ensure_active_admin_remains(db, user, payload)
    if payload.role is not None:
        user.role = normalize_user_role_input(payload.role)
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.temporary_password:
        ensure_password_strength(payload.temporary_password)
        user.password_hash = hash_password(payload.temporary_password)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_user_write_error(exc)
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account while signed in")
    if canonicalize_user_role(user.role) == "admin" and user.is_active:
        active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
        if active_admin_count <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")
    db.delete(user)
    db.commit()
    return {"ok": True}


@app.get("/api/schedules", response_model=list[ScheduleRunMetaOut])
def list_schedules(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[ScheduleRunMetaOut]:
    rows = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()
    return [serialize_schedule_meta(run, email) for run, email in rows]


@app.post("/api/schedules", response_model=ScheduleRunMetaOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduleSavePayload,
    current_user: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> ScheduleRunMetaOut:
    schedule_run = ScheduleRun(
        created_by_user_id=current_user.id,
        period_start=payload.period_start,
        weeks=payload.weeks,
        label=payload.label,
        payload_json=payload.payload_json,
        result_json=payload.result_json,
    )
    db.add(schedule_run)
    db.commit()
    db.refresh(schedule_run)
    return serialize_schedule_meta(schedule_run, current_user.email)


@app.get("/api/schedules/{schedule_id}", response_model=ScheduleRunOut)
def get_schedule(
    schedule_id: int,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> ScheduleRunOut:
    row = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .where(ScheduleRun.id == schedule_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved schedule not found")
    run, email = row
    return serialize_schedule_out(run, email)


@app.get("/api/view-only/schedules", response_model=list[ViewOnlyScheduleOut])
def list_view_only_schedules(
    _: User = Depends(get_view_only_user),
    db: Session = Depends(get_db),
) -> list[ViewOnlyScheduleOut]:
    rows = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
        .limit(2)
    ).all()
    return [serialize_view_only_schedule(run, email) for run, email in rows]


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    schedule_run = db.get(ScheduleRun, schedule_id)
    if schedule_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved schedule not found")
    db.delete(schedule_run)
    db.commit()
    return {"ok": True}


@app.delete("/api/schedules")
def delete_all_schedules(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int | bool]:
    result = db.execute(delete(ScheduleRun))
    db.commit()
    deleted_count = int(result.rowcount or 0)
    return {"ok": True, "deleted": deleted_count}


@app.post("/settings")
def update_settings_compat():
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "env": os.getenv("ENVIRONMENT", "local")}


@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    current_user = get_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
    if current_user is not None and canonicalize_user_role(current_user.role) == "view_only":
        return RedirectResponse(url="/viewer", status_code=status.HTTP_303_SEE_OTHER)
    payload_json = json.dumps(_sample_payload_dict())
    return templates.TemplateResponse(request, "pages/index.html", {"request": request, "payload_json": payload_json})


@app.get("/viewer")
def view_only_dashboard(request: Request, db: Session = Depends(get_db)):
    current_user = get_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
    if current_user is None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if canonicalize_user_role(current_user.role) != "view_only":
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "pages/view_only.html", {"request": request})


@app.post("/generate", response_model=GenerateResponse)
def generate(
    payload: GenerateRequest,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> GenerateResponse:
    global _LAST_RESULT
    if payload.period.start_date < date.today():
        raise HTTPException(status_code=400, detail="Start date cannot be in the past")
    history_weekly_hours, history_weekly_leader_days, history_weekly_work_days = _load_generation_history_maps(db, payload)
    _LAST_RESULT = _generate(
        payload,
        history_weekly_hours=history_weekly_hours,
        history_weekly_leader_days=history_weekly_leader_days,
        history_weekly_work_days=history_weekly_work_days,
    )
    return _LAST_RESULT


@app.get("/export/json")
def export_json(_: User = Depends(get_manager_or_admin_user)) -> JSONResponse:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    return JSONResponse(content=_LAST_RESULT.model_dump())


@app.get("/export/csv")
def export_csv(_: User = Depends(get_manager_or_admin_user)) -> Response:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "location", "start", "end", "employee_id", "employee_name", "role"])
    for assignment in _LAST_RESULT.assignments:
        writer.writerow([assignment.date, assignment.location, assignment.start, assignment.end, assignment.employee_id, assignment.employee_name, assignment.role])
    return Response(content=out.getvalue(), media_type="text/csv")
