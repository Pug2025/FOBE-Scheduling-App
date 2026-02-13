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
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EmployeeRecord, ScheduleRun, SessionRecord, User
from app.security import hash_password, verify_password

app = FastAPI(title="FOBE Scheduler Prototype")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SESSION_COOKIE_NAME = "session_id"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60


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
    role: Literal["admin", "user"] = "user"


class UserPatchPayload(BaseModel):
    role: Literal["admin", "user"] | None = None
    temporary_password: str | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: int
    email: str
    role: Literal["admin", "user"]
    is_active: bool
    created_at: datetime

    @classmethod
    def from_orm_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            role=user.role,
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


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
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def ensure_active_admin_remains(db: Session, target_user: User, patch: UserPatchPayload) -> None:
    next_role = patch.role if patch.role is not None else target_user.role
    next_is_active = patch.is_active if patch.is_active is not None else target_user.is_active
    if target_user.role != "admin" or target_user.is_active is False:
        return
    if next_role == "admin" and next_is_active:
        return
    active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
    if active_admin_count <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")


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
    availability: dict[str, list[str]]


class Unavailability(BaseModel):
    employee_id: str
    date: date
    reason: str = ""


class ExtraCoverageDay(BaseModel):
    date: date
    extra_people: int = 1


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
    extra_coverage_days: list[ExtraCoverageDay] = Field(default_factory=list)
    history: History = Field(default_factory=History)
    open_weekdays: list[DayKey] = Field(default_factory=lambda: DAY_KEYS.copy())
    week_start_day: DayKey = "sun"
    week_end_day: DayKey = "sat"
    reroll_token: int = Field(default=0, ge=0)
    schedule_beach_shop: bool = False

    @model_validator(mode="after")
    def validate_week_boundaries(self) -> GenerateRequest:
        start_idx = DAY_KEYS.index(self.week_start_day)
        end_idx = DAY_KEYS.index(self.week_end_day)
        if (end_idx - start_idx) % 7 != 6:
            raise ValueError("week_start_day and week_end_day must define a full 7-day week boundary")
        return self


class AssignmentOut(BaseModel):
    date: str
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    employee_name: str
    role: Role


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


def _generate(payload: GenerateRequest) -> GenerateResponse:
    start_date = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    season_rules = _normalized_season_rules(start_date, payload.season_rules)
    emp_map = {e.id: e for e in sorted(payload.employees, key=lambda x: x.id)}
    unavail = {(u.employee_id, u.date) for u in payload.unavailability}
    extras = {x.date: x.extra_people for x in payload.extra_coverage_days}
    all_days = _daterange(start_date, payload.period.weeks * 7)
    week_starts = [start_date + timedelta(days=7 * i) for i in range(payload.period.weeks)]
    open_weekdays = set(payload.open_weekdays or DAY_KEYS)

    def is_store_open(day: date) -> bool:
        return DAY_KEYS[day.weekday()] in open_weekdays

    open_days = [d for d in all_days if is_store_open(d)]
    open_day_index = {d: i for i, d in enumerate(open_days)}

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_assigned: dict[date, set[str]] = defaultdict(set)
    daily_hours_counted: dict[tuple[str, date], float] = defaultdict(float)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
    weekly_days: dict[tuple[str, int], int] = defaultdict(int)
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
    if payload.leadership_rules.manager_two_consecutive_days_off_per_week and manager_ids:
        for ws in week_starts:
            week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
            if week_days:
                for manager_id in manager_ids:
                    if manager_vacations_by_week[(manager_id, ws)] > 0:
                        continue
                    week_open_days = [d for d in week_days if is_store_open(d)]
                    if len(week_open_days) < 2:
                        continue
                    a, b = _choose_pair_for_manager_off(week_open_days, season_rules, extras)
                    forced_manager_off.update({a, b})

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

    def eligible(day: date, role: Role, start: str, end: str, ignore_max: bool = False, allow_double_booking: bool = False) -> list[Employee]:
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        out: list[Employee] = []
        for e in emp_map.values():
            if e.role != role:
                continue
            if (e.id, day) in unavail:
                continue
            if not allow_double_booking and e.id in daily_assigned[day]:
                continue
            if role == "Store Manager" and day in forced_manager_off:
                continue
            wk = _week_index(day, start_date)
            if role == "Store Manager" and weekly_days[(e.id, wk)] >= 5:
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

        wk = _week_index(day, start_date)
        out.sort(key=lambda e: (
            off_streak_priority(e.id),
            work_pattern_penalty(e.id),
            weekly_hours[(e.id, wk)],
            PRIORITY_ORDER[e.priority_tier],
            _reroll_rank(e.id, payload.reroll_token),
            e.name,
        ))
        return out

    def add_assignment(day: date, location: str, start: str, end: str, employee: Employee, role: Role):
        assignments.append({
            "date": day,
            "location": location,
            "start": start,
            "end": end,
            "employee_id": employee.id,
            "employee_name": employee.name,
            "role": role,
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
        if employee.role == "Store Manager" and weekly_days[(employee.id, wk)] >= 5:
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

    for d in all_days:
        if is_store_open(d):
            g_start, g_end = payload.hours.greystones.start, payload.hours.greystones.end
            needed = (payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff) + extras.get(d, 0)
            assign_one(d, "Greystones", g_start, g_end, "Store Manager", 1)
            manager_on = any(a for a in assignments if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Store Manager")
            weekend_manager_off = _is_weekend(d) and not manager_on
            lead_need = max(payload.leadership_rules.min_team_leaders_every_open_day, 2 if weekend_manager_off else 1)
            # Weekend manager-off rule should not be blocked by weekly max-hours limits.
            assign_one(d, "Greystones", g_start, g_end, "Team Leader", lead_need, ignore_max=weekend_manager_off)

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
            before = len([a for a in assignments if a["date"] == d and a["location"] == "Beach Shop"])
            assign_one(d, "Beach Shop", b_start, b_end, "Store Clerk", needed, ignore_max=True, allow_double_booking=True)
            now = len([a for a in assignments if a["date"] == d and a["location"] == "Beach Shop"])
            if now < needed:
                assign_one(d, "Beach Shop", b_start, b_end, "Team Leader", needed - now, ignore_max=True, allow_double_booking=True)
            final = len([a for a in assignments if a["date"] == d and a["location"] == "Beach Shop"])
            if final - before < needed:
                violations.append(ViolationOut(date=d.isoformat(), type="beach_shop_gap", detail=f"Beach Shop needed {needed}"))

    # Meet weekly minimums even if that means exceeding baseline daily coverage.
    # Make-up day preference is role-specific (e.g., Team Leader Sat/Fri, Store Clerk Thu/Fri).
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
            if payload.leadership_rules.manager_two_consecutive_days_off_per_week and not has_pair:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_consecutive_days_off", detail=f"Manager {emp_map[manager_id].name} lacks consecutive days off"))
            requested_days_off = sum(1 for d in week_days if (manager_id, d) in unavail)
            target_days = max(0, min(5, len(week_days) - requested_days_off))
            actual_days = sum(work)
            if actual_days < target_days:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_days_rule", detail=f"Manager {emp_map[manager_id].name} scheduled {actual_days} day(s), minimum is {target_days}"))

    for ws in week_starts:
        wk = _week_index(ws, start_date)
        for e in emp_map.values():
            scheduled_hours = round(weekly_hours[(e.id, wk)], 2)
            if scheduled_hours < e.min_hours_per_week and requested_days_off_by_week[(e.id, wk)] == 0:
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
            {"id": "manager_mia", "name": "Manager Mia", "role": "Store Manager", "min_hours_per_week": 24, "max_hours_per_week": 40, "priority_tier": "A", "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "taylor", "name": "Taylor", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "A", "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "sam", "name": "Sam", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "casey", "name": "Casey", "role": "Boat Captain", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "jordan", "name": "Jordan", "role": "Store Clerk", "min_hours_per_week": 16, "max_hours_per_week": 40, "priority_tier": "B", "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
        ],
        "unavailability": [],
        "extra_coverage_days": [{"date": "2025-07-12", "extra_people": 1}],
        "history": {"manager_weekends_worked_this_month": 0},
        "open_weekdays": DAY_KEYS,
        "week_start_day": "sun",
        "week_end_day": "sat",
        "reroll_token": 0,
        "schedule_beach_shop": False,
    }

def serialize_employee_record(record: EmployeeRecord) -> Employee:
    return Employee(
        id=record.employee_id,
        name=record.name,
        role=record.role,
        min_hours_per_week=record.min_hours_per_week,
        max_hours_per_week=record.max_hours_per_week,
        priority_tier=record.priority_tier,
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
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Employee]:
    records = db.scalars(select(EmployeeRecord).order_by(EmployeeRecord.sort_order, EmployeeRecord.id)).all()
    return serialize_roster(list(records))


@app.put("/api/employees", response_model=list[Employee])
def put_employees(
    employees: list[Employee] = Body(...),
    _: User = Depends(get_admin_user),
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
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")
    user = User(
        email=email,
        password_hash=hash_password(payload.temporary_password),
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.patch("/api/admin/users/{user_id}", response_model=UserOut)
def admin_patch_user(
    user_id: int,
    payload: UserPatchPayload,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if payload.role is None and payload.temporary_password is None and payload.is_active is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updates were provided")
    ensure_active_admin_remains(db, user, payload)
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.temporary_password:
        ensure_password_strength(payload.temporary_password)
        user.password_hash = hash_password(payload.temporary_password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.get("/api/schedules", response_model=list[ScheduleRunMetaOut])
def list_schedules(
    _: User = Depends(get_current_user),
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
    current_user: User = Depends(get_admin_user),
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
    _: User = Depends(get_current_user),
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


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    _: User = Depends(get_admin_user),
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
    _: User = Depends(get_admin_user),
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
def index(request: Request):
    payload_json = json.dumps(_sample_payload_dict())
    return templates.TemplateResponse(request, "pages/index.html", {"request": request, "payload_json": payload_json})


@app.post("/generate", response_model=GenerateResponse)
def generate(payload: GenerateRequest, _: User = Depends(get_current_user)) -> GenerateResponse:
    global _LAST_RESULT
    if payload.period.start_date < date.today():
        raise HTTPException(status_code=400, detail="Start date cannot be in the past")
    _LAST_RESULT = _generate(payload)
    return _LAST_RESULT


@app.get("/export/json")
def export_json(_: User = Depends(get_current_user)) -> JSONResponse:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    return JSONResponse(content=_LAST_RESULT.model_dump())


@app.get("/export/csv")
def export_csv(_: User = Depends(get_current_user)) -> Response:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "location", "start", "end", "employee_id", "employee_name", "role"])
    for assignment in _LAST_RESULT.assignments:
        writer.writerow([assignment.date, assignment.location, assignment.start, assignment.end, assignment.employee_id, assignment.employee_name, assignment.role])
    return Response(content=out.getvalue(), media_type="text/csv")
