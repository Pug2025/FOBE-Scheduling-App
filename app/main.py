from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="FOBE Scheduler Prototype")
templates = Jinja2Templates(directory="templates")


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
    type: Literal["coverage_gap", "leader_gap", "manager_consecutive_days_off", "role_missing", "beach_shop_gap", "manager_days_rule"]
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

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_assigned: dict[date, set[str]] = defaultdict(set)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
    weekly_days: dict[tuple[str, int], int] = defaultdict(int)

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
                    open_days = [d for d in week_days if is_store_open(d)]
                    if len(open_days) < 2:
                        continue
                    a, b = _choose_pair_for_manager_off(open_days, season_rules, extras)
                    forced_manager_off.update({a, b})

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
        out.sort(key=lambda e: (
            PRIORITY_ORDER[e.priority_tier],
            weekly_hours[(e.id, _week_index(day, start_date))],
            int((day - timedelta(days=1)) in all_days and e.id in daily_assigned[day - timedelta(days=1)]),
            _reroll_rank(e.id, payload.reroll_token),
            e.name,
        ))
        return out

    def assign_one(day: date, location: str, start: str, end: str, role: Role, needed: int, ignore_max: bool = False, allow_double_booking: bool = False):
        for e in eligible(day, role, start, end, ignore_max=ignore_max, allow_double_booking=allow_double_booking)[:needed]:
            assignments.append({
                "date": day,
                "location": location,
                "start": start,
                "end": end,
                "employee_id": e.id,
                "employee_name": e.name,
                "role": role,
            })
            wk = _week_index(day, start_date)
            weekly_hours[(e.id, wk)] += _hours_between(start, end)
            if e.id not in daily_assigned[day]:
                weekly_days[(e.id, wk)] += 1
            daily_assigned[day].add(e.id)

    for d in all_days:
        if is_store_open(d):
            g_start, g_end = payload.hours.greystones.start, payload.hours.greystones.end
            needed = (payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff) + extras.get(d, 0)
            assign_one(d, "Greystones", g_start, g_end, "Store Manager", 1)
            manager_on = any(a for a in assignments if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Store Manager")
            lead_need = max(payload.leadership_rules.min_team_leaders_every_open_day, 2 if (_is_weekend(d) and not manager_on) else 1)
            assign_one(d, "Greystones", g_start, g_end, "Team Leader", lead_need)

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

            # Captain must always be assigned when open, even if max hours exceeded.
            captain = eligible(d, "Boat Captain", g_start, g_end, ignore_max=True)[:1]
            if captain:
                e = captain[0]
                assignments.append({
                    "date": d,
                    "location": "Boat",
                    "start": g_start,
                    "end": g_end,
                    "employee_id": e.id,
                    "employee_name": e.name,
                    "role": "Boat Captain",
                })
                wk = _week_index(d, start_date)
                weekly_hours[(e.id, wk)] += _hours_between(g_start, g_end)
                if e.id not in daily_assigned[d]:
                    weekly_days[(e.id, wk)] += 1
                daily_assigned[d].add(e.id)
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

    totals: dict[str, TotalsOut] = {e.id: TotalsOut() for e in emp_map.values()}
    for e in emp_map.values():
        totals[e.id].week1_hours = round(weekly_hours[(e.id, 1)], 2)
        totals[e.id].week2_hours = round(weekly_hours[(e.id, 2)], 2)

    weekend_days_by_employee: dict[str, set[date]] = defaultdict(set)
    for a in assignments:
        wk = _week_index(a["date"], start_date)
        day_credit = 0.5 if a["location"] == "Beach Shop" else 1.0
        if wk == 1:
            totals[a["employee_id"]].week1_days += day_credit
        elif wk == 2:
            totals[a["employee_id"]].week2_days += day_credit
        if _is_weekend(a["date"]):
            weekend_days_by_employee[a["employee_id"]].add(a["date"])

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




@app.post("/settings")
def update_settings_compat():
    return RedirectResponse(url="/", status_code=303)
@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "env": "local"}


@app.get("/")
def index(request: Request):
    payload_json = json.dumps(_sample_payload_dict())
    return templates.TemplateResponse(request, "pages/index.html", {"request": request, "payload_json": payload_json})


@app.post("/generate", response_model=GenerateResponse)
def generate(payload: GenerateRequest) -> GenerateResponse:
    global _LAST_RESULT
    if payload.period.start_date < date.today():
        raise HTTPException(status_code=400, detail="Start date cannot be in the past")
    _LAST_RESULT = _generate(payload)
    return _LAST_RESULT


@app.get("/export/json")
def export_json() -> JSONResponse:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    return JSONResponse(content=_LAST_RESULT.model_dump())


@app.get("/export/csv")
def export_csv() -> Response:
    if _LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No generated schedule available")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "location", "start", "end", "employee_id", "employee_name", "role"])
    for a in _LAST_RESULT.assignments:
        writer.writerow([a.date, a.location, a.start, a.end, a.employee_id, a.employee_name, a.role])
    return Response(content=out.getvalue(), media_type="text/csv")
