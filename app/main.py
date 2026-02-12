from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="FOBE Scheduler Prototype")


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


class Employee(BaseModel):
    id: str
    name: str
    roles: list[Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]]
    min_hours_per_week: int
    max_hours_per_week: int
    priority_tier: Literal["A", "B", "C"]
    availability: dict[str, list[str]]


class Unavailability(BaseModel):
    employee_id: str
    date: date
    reason: str = ""


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
    history: History = Field(default_factory=History)


class AssignmentOut(BaseModel):
    date: str
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    role: Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]


class TotalsOut(BaseModel):
    week1_hours: float = 0
    week2_hours: float = 0
    week1_days: int = 0
    week2_days: int = 0
    weekend_days: int = 0
    locations: dict[str, int] = Field(default_factory=lambda: {"Greystones": 0, "Beach Shop": 0, "Boat": 0})


class ViolationOut(BaseModel):
    date: str
    type: Literal["coverage_gap", "leader_gap", "manager_consecutive_days_off", "max_hours", "role_missing", "beach_shop_gap"]
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
    return (_time_to_minutes(end) - _time_to_minutes(start)) / 60.0


def _daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


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
    return july_1 <= d <= s.labour_day and d.weekday() >= 5


def _slot(start: str, end: str, location: str, needed: int, allowed_roles: list[str], violation_type: str):
    return {
        "start": start,
        "end": end,
        "location": location,
        "needed": needed,
        "allowed_roles": allowed_roles,
        "violation_type": violation_type,
    }


def _generate(payload: GenerateRequest) -> GenerateResponse:
    emp_map = {e.id: e for e in sorted(payload.employees, key=lambda x: x.id)}
    unavail = {(u.employee_id, u.date) for u in payload.unavailability}
    start_date = payload.period.start_date
    days = payload.period.weeks * 7
    all_days = _daterange(start_date, days)

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_spans: dict[tuple[str, date], list[tuple[int, int]]] = defaultdict(list)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
    daily_assigned: dict[date, set[str]] = defaultdict(set)

    for d in all_days:
        slots = []
        if _is_greystones_open(d, payload.season_rules):
            staff_needed = payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff
            slots.append(_slot(payload.hours.greystones.start, payload.hours.greystones.end, "Greystones", staff_needed, ["Store Clerk", "Team Leader", "Store Manager"], "coverage_gap"))
            slots.append(_slot(payload.hours.greystones.start, payload.hours.greystones.end, "Boat", 1, ["Boat Captain"], "role_missing"))
        if _is_beach_shop_open(d, payload.season_rules):
            slots.append(_slot(payload.hours.beach_shop.start, payload.hours.beach_shop.end, "Beach Shop", payload.coverage.beach_shop_staff, ["Store Clerk", "Team Leader", "Store Manager"], "beach_shop_gap"))

        for slot in slots:
            slot_hours = _hours_between(slot["start"], slot["end"])
            smin = _time_to_minutes(slot["start"])
            emin = _time_to_minutes(slot["end"])
            needed = slot["needed"]
            eligible = []
            for e in emp_map.values():
                if (e.id, d) in unavail:
                    continue
                day_key = DAY_KEYS[d.weekday()]
                windows = e.availability.get(day_key, [])
                fits_window = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
                if not fits_window:
                    continue
                if not any(r in slot["allowed_roles"] for r in e.roles):
                    continue
                week_idx = ((d - start_date).days // 7) + 1
                if weekly_hours[(e.id, week_idx)] + slot_hours > e.max_hours_per_week:
                    continue
                overlap = False
                for astart, aend in daily_spans[(e.id, d)]:
                    if max(astart, smin) < min(aend, emin):
                        overlap = True
                        break
                if overlap:
                    continue
                assigned_hours = weekly_hours[(e.id, week_idx)]
                assigned_days = sum(1 for day in all_days if e.id in daily_assigned[day])
                eligible.append((PRIORITY_ORDER[e.priority_tier], assigned_hours, assigned_days, e.id, e))

            eligible.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
            chosen = [x[4] for x in eligible[:needed]]

            for e in chosen:
                assigned_role = next(r for r in ["Store Manager", "Team Leader", "Store Clerk", "Boat Captain"] if r in e.roles and r in slot["allowed_roles"])
                assignments.append(
                    {
                        "date": d,
                        "location": slot["location"],
                        "start": slot["start"],
                        "end": slot["end"],
                        "employee_id": e.id,
                        "role": assigned_role,
                    }
                )
                week_idx = ((d - start_date).days // 7) + 1
                weekly_hours[(e.id, week_idx)] += slot_hours
                daily_spans[(e.id, d)].append((smin, emin))
                daily_assigned[d].add(e.id)

            if len(chosen) < needed:
                violations.append(
                    ViolationOut(
                        date=d.isoformat(),
                        type=slot["violation_type"],
                        detail=f"{slot['location']} needed {needed}, assigned {len(chosen)}",
                    )
                )

        if _is_greystones_open(d, payload.season_rules):
            day_assignments = [a for a in assignments if a["date"] == d and a["location"] == "Greystones"]
            leaders = [a for a in day_assignments if a["role"] == "Team Leader"]
            managers = [a for a in day_assignments if a["role"] == "Store Manager"]
            if len(leaders) < payload.leadership_rules.min_team_leaders_every_open_day:
                violations.append(ViolationOut(date=d.isoformat(), type="leader_gap", detail="Missing minimum team leader coverage"))
            if _is_weekend(d) and not managers and len(leaders) < payload.leadership_rules.weekend_team_leaders_if_manager_off:
                violations.append(ViolationOut(date=d.isoformat(), type="leader_gap", detail="Weekend needs 2 Team Leaders when manager off"))

    manager_ids = sorted([e.id for e in emp_map.values() if "Store Manager" in e.roles])
    for week_start in _daterange(start_date, days):
        if week_start.weekday() != 0:
            continue
        week_days = [week_start + timedelta(days=i) for i in range(7) if (week_start + timedelta(days=i)) in all_days]
        for manager_id in manager_ids:
            work = [manager_id in daily_assigned[d] for d in week_days]
            has_pair = any((not work[i]) and (not work[i + 1]) for i in range(len(work) - 1))
            if payload.leadership_rules.manager_two_consecutive_days_off_per_week and not has_pair:
                violations.append(
                    ViolationOut(
                        date=week_start.isoformat(),
                        type="manager_consecutive_days_off",
                        detail=f"Manager {manager_id} lacks consecutive days off in week",
                    )
                )

    totals: dict[str, TotalsOut] = {e.id: TotalsOut() for e in emp_map.values()}
    for e in emp_map.values():
        for d in all_days:
            week_idx = ((d - start_date).days // 7) + 1
            if e.id in daily_assigned[d]:
                if week_idx == 1:
                    totals[e.id].week1_days += 1
                elif week_idx == 2:
                    totals[e.id].week2_days += 1
                if _is_weekend(d):
                    totals[e.id].weekend_days += 1
        totals[e.id].week1_hours = round(weekly_hours[(e.id, 1)], 2)
        totals[e.id].week2_hours = round(weekly_hours[(e.id, 2)], 2)

    for a in assignments:
        totals[a["employee_id"]].locations[a["location"]] += 1

    for e in emp_map.values():
        for week in [1, 2]:
            if weekly_hours[(e.id, week)] < e.min_hours_per_week:
                week_start = start_date + timedelta(days=(week - 1) * 7)
                violations.append(
                    ViolationOut(
                        date=week_start.isoformat(),
                        type="coverage_gap",
                        detail=f"{e.id} below min_hours_per_week in week {week}",
                    )
                )

    out_assignments = [
        AssignmentOut(
            date=a["date"].isoformat(),
            location=a["location"],
            start=a["start"],
            end=a["end"],
            employee_id=a["employee_id"],
            role=a["role"],
        )
        for a in sorted(assignments, key=lambda x: (x["date"], x["location"], x["start"], x["employee_id"]))
    ]
    out_violations = sorted(violations, key=lambda v: (v.date, v.type, v.detail))
    return GenerateResponse(assignments=out_assignments, totals_by_employee=totals, violations=out_violations)


def _sample_payload() -> str:
    return json.dumps(
        {
            "period": {"start_date": "2025-07-07", "weeks": 2},
            "season_rules": {
                "victoria_day": "2025-05-19",
                "june_30": "2025-06-30",
                "labour_day": "2025-09-01",
                "oct_31": "2025-10-31",
            },
            "hours": {
                "greystones": {"start": "08:30", "end": "17:30"},
                "beach_shop": {"start": "12:00", "end": "16:00"},
            },
            "coverage": {"greystones_weekday_staff": 3, "greystones_weekend_staff": 4, "beach_shop_staff": 2},
            "leadership_rules": {
                "min_team_leaders_every_open_day": 1,
                "weekend_team_leaders_if_manager_off": 2,
                "manager_two_consecutive_days_off_per_week": True,
                "manager_min_weekends_per_month": 2,
            },
            "employees": [
                {
                    "id": "e1",
                    "name": "Manager Mia",
                    "roles": ["Store Manager", "Team Leader", "Store Clerk"],
                    "min_hours_per_week": 24,
                    "max_hours_per_week": 40,
                    "priority_tier": "A",
                    "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
                },
                {
                    "id": "e2",
                    "name": "Taylor",
                    "roles": ["Team Leader", "Store Clerk"],
                    "min_hours_per_week": 20,
                    "max_hours_per_week": 40,
                    "priority_tier": "A",
                    "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
                },
                {
                    "id": "e3",
                    "name": "Casey",
                    "roles": ["Boat Captain", "Store Clerk"],
                    "min_hours_per_week": 20,
                    "max_hours_per_week": 40,
                    "priority_tier": "B",
                    "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
                },
                {
                    "id": "e4",
                    "name": "Jordan",
                    "roles": ["Store Clerk"],
                    "min_hours_per_week": 16,
                    "max_hours_per_week": 40,
                    "priority_tier": "B",
                    "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
                },
                {
                    "id": "e5",
                    "name": "Riley",
                    "roles": ["Store Clerk"],
                    "min_hours_per_week": 16,
                    "max_hours_per_week": 40,
                    "priority_tier": "C",
                    "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
                },
            ],
            "unavailability": [{"employee_id": "e2", "date": "2025-07-12", "reason": "Vacation"}],
            "history": {"manager_weekends_worked_this_month": 0},
        },
        indent=2,
    )


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    payload = _sample_payload()
    export_links = ""
    if _LAST_RESULT is not None:
        export_links = '<p><a href="/export/json">Download JSON</a> | <a href="/export/csv">Download CSV</a></p>'
    return f"""
<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>FOBE Scheduler</title></head>
<body style=\"font-family: sans-serif; max-width: 1100px; margin: 2rem auto;\">
  <h1>FOBE 2-Week Schedule Generator</h1>
  <p>Paste/edit payload JSON and generate a deterministic schedule.</p>
  <textarea id=\"payload\" style=\"width:100%;height:360px;\">{payload}</textarea><br/>
  <button onclick=\"runGenerate()\">Generate 2-week schedule</button>
  {export_links}
  <h2>Results</h2>
  <div id=\"result\"></div>
<script>
async function runGenerate() {{
  const raw = document.getElementById('payload').value;
  let data;
  try {{ data = JSON.parse(raw); }} catch (e) {{ document.getElementById('result').innerText = 'Invalid JSON: ' + e; return; }}
  const res = await fetch('/generate', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(data) }});
  const json = await res.json();
  if (!res.ok) {{ document.getElementById('result').innerText = JSON.stringify(json, null, 2); return; }}
  const rows = json.assignments.map(a => `<tr><td>${{a.date}}</td><td>${{a.location}}</td><td>${{a.start}}-${{a.end}}</td><td>${{a.employee_id}}</td><td>${{a.role}}</td></tr>`).join('');
  const viol = json.violations.length ? `<ul>${{json.violations.map(v => `<li>${{v.date}} | ${{v.type}} | ${{v.detail}}</li>`).join('')}}</ul>` : '<p>None</p>';
  document.getElementById('result').innerHTML = `<p><a href=\"/export/json\">Download JSON</a> | <a href=\"/export/csv\">Download CSV</a></p><table border=1 cellpadding=4 cellspacing=0><thead><tr><th>Date</th><th>Location</th><th>Time</th><th>Employee</th><th>Role</th></tr></thead><tbody>${{rows}}</tbody></table><h3>Violations</h3>${{viol}}`;
}}
</script>
</body>
</html>
"""


@app.post("/generate", response_model=GenerateResponse)
def generate(payload: GenerateRequest) -> GenerateResponse:
    global _LAST_RESULT
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
    writer.writerow(["date", "location", "start", "end", "employee_id", "role"])
    for a in _LAST_RESULT.assignments:
        writer.writerow([a.date, a.location, a.start, a.end, a.employee_id, a.role])
    return Response(content=out.getvalue(), media_type="text/csv")
