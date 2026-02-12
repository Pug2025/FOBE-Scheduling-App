from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="FOBE Scheduler Prototype")


class Period(BaseModel):
    start_date: date
    weeks: int = 2


class SeasonRules(BaseModel):
    victoria_day: date
    june_30: date
    labour_day: date
    oct_31: date


class HoursWindow(BaseModel):
    start: str
    end: str


class Hours(BaseModel):
    greystones: HoursWindow
    beach_shop: HoursWindow


class Coverage(BaseModel):
    greystones_weekday_staff: int = 3
    greystones_weekend_staff: int = 4
    beach_shop_staff: int = 2


class LeadershipRules(BaseModel):
    min_team_leaders_every_open_day: int = 1
    weekend_team_leaders_if_manager_off: int = 2
    manager_two_consecutive_days_off_per_week: bool = True
    manager_min_weekends_per_month: int = 2


class EmployeeAvailability(BaseModel):
    mon: List[str] = Field(default_factory=list)
    tue: List[str] = Field(default_factory=list)
    wed: List[str] = Field(default_factory=list)
    thu: List[str] = Field(default_factory=list)
    fri: List[str] = Field(default_factory=list)
    sat: List[str] = Field(default_factory=list)
    sun: List[str] = Field(default_factory=list)


class Employee(BaseModel):
    id: str
    name: str
    roles: List[Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]]
    min_hours_per_week: int = 0
    max_hours_per_week: int = 40
    priority_tier: Literal["A", "B", "C"]
    availability: EmployeeAvailability


class UnavailabilityEntry(BaseModel):
    employee_id: str
    date: date
    reason: str


class History(BaseModel):
    manager_weekends_worked_this_month: int = 0


class GenerateRequest(BaseModel):
    period: Period
    season_rules: SeasonRules
    hours: Hours
    coverage: Coverage
    leadership_rules: LeadershipRules
    employees: List[Employee]
    unavailability: List[UnavailabilityEntry] = Field(default_factory=list)
    history: History


class Assignment(BaseModel):
    date: str
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    role: Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]


SAMPLE_PAYLOAD = {
    "period": {"start_date": "2025-07-01", "weeks": 2},
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
    "coverage": {
        "greystones_weekday_staff": 3,
        "greystones_weekend_staff": 4,
        "beach_shop_staff": 2,
    },
    "leadership_rules": {
        "min_team_leaders_every_open_day": 1,
        "weekend_team_leaders_if_manager_off": 2,
        "manager_two_consecutive_days_off_per_week": True,
        "manager_min_weekends_per_month": 2,
    },
    "employees": [
        {
            "id": "e1",
            "name": "Morgan Manager",
            "roles": ["Store Manager", "Team Leader", "Store Clerk"],
            "min_hours_per_week": 24,
            "max_hours_per_week": 45,
            "priority_tier": "A",
            "availability": {
                "mon": ["08:30-17:30"],
                "tue": ["08:30-17:30"],
                "wed": ["08:30-17:30"],
                "thu": ["08:30-17:30"],
                "fri": ["08:30-17:30"],
                "sat": ["08:30-17:30"],
                "sun": ["08:30-17:30"],
            },
        },
        {
            "id": "e2",
            "name": "Taylor Leader",
            "roles": ["Team Leader", "Store Clerk"],
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "availability": {
                "mon": ["08:30-17:30"],
                "tue": ["08:30-17:30"],
                "wed": ["08:30-17:30"],
                "thu": ["08:30-17:30"],
                "fri": ["08:30-17:30"],
                "sat": ["08:30-17:30"],
                "sun": ["08:30-17:30"],
            },
        },
        {
            "id": "e3",
            "name": "Riley Captain",
            "roles": ["Boat Captain", "Store Clerk"],
            "min_hours_per_week": 16,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "availability": {
                "mon": ["08:30-17:30"],
                "tue": ["08:30-17:30"],
                "wed": ["08:30-17:30"],
                "thu": ["08:30-17:30"],
                "fri": ["08:30-17:30"],
                "sat": ["08:30-17:30"],
                "sun": ["08:30-17:30"],
            },
        },
        {
            "id": "e4",
            "name": "Casey Clerk",
            "roles": ["Store Clerk"],
            "min_hours_per_week": 12,
            "max_hours_per_week": 30,
            "priority_tier": "B",
            "availability": {
                "mon": ["08:30-17:30"],
                "tue": ["08:30-17:30"],
                "wed": ["08:30-17:30"],
                "thu": ["08:30-17:30"],
                "fri": ["08:30-17:30"],
                "sat": ["08:30-17:30"],
                "sun": ["08:30-17:30"],
            },
        },
        {
            "id": "e5",
            "name": "Jordan Flex",
            "roles": ["Store Clerk", "Team Leader"],
            "min_hours_per_week": 10,
            "max_hours_per_week": 30,
            "priority_tier": "C",
            "availability": {
                "mon": ["08:30-17:30"],
                "tue": ["08:30-17:30"],
                "wed": ["08:30-17:30"],
                "thu": ["08:30-17:30"],
                "fri": ["08:30-17:30"],
                "sat": ["08:30-17:30"],
                "sun": ["08:30-17:30"],
            },
        },
    ],
    "unavailability": [
        {"employee_id": "e4", "date": "2025-07-05", "reason": "Vacation"}
    ],
    "history": {"manager_weekends_worked_this_month": 0},
}

LAST_RESULT: dict | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def parse_hhmm(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def duration_hours(start: str, end: str) -> float:
    return (parse_hhmm(end) - parse_hhmm(start)) / 60.0


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def day_key(day: date) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]


def daterange(start: date, days: int) -> List[date]:
    return [start + timedelta(days=i) for i in range(days)]


def in_range(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def greystones_open(day: date, season: SeasonRules) -> bool:
    july_1 = date(day.year, 7, 1)
    if in_range(day, season.victoria_day, season.june_30):
        return day.weekday() in {4, 5, 6}
    if in_range(day, july_1, season.labour_day):
        return True
    if in_range(day, season.labour_day + timedelta(days=1), season.oct_31):
        return day.weekday() in {4, 5, 6}
    return False


def beach_shop_open(day: date, season: SeasonRules) -> bool:
    july_1 = date(day.year, 7, 1)
    return in_range(day, july_1, season.labour_day) and day.weekday() in {5, 6}


def has_time_window(windows: List[str], start: str, end: str) -> bool:
    need_start = parse_hhmm(start)
    need_end = parse_hhmm(end)
    for window in windows:
        window_start, window_end = window.split("-")
        ws = parse_hhmm(window_start)
        we = parse_hhmm(window_end)
        if ws <= need_start and we >= need_end:
            return True
    return False


def build_schedule(payload: GenerateRequest) -> dict:
    employees = {e.id: e for e in sorted(payload.employees, key=lambda emp: emp.id)}
    unavailability = {(u.employee_id, u.date.isoformat()) for u in payload.unavailability}

    assignments: List[dict] = []
    violations: List[dict] = []

    days = daterange(payload.period.start_date, payload.period.weeks * 7)

    week_hours: Dict[str, List[float]] = {eid: [0.0 for _ in range(payload.period.weeks)] for eid in employees}
    week_days: Dict[str, List[int]] = {eid: [0 for _ in range(payload.period.weeks)] for eid in employees}
    weekend_days: Dict[str, int] = {eid: 0 for eid in employees}
    locations: Dict[str, Dict[str, int]] = {
        eid: {"Greystones": 0, "Beach Shop": 0, "Boat": 0} for eid in employees
    }
    worked_day: Dict[tuple, bool] = defaultdict(bool)

    tier_rank = {"A": 0, "B": 1, "C": 2}

    def week_index(day: date) -> int:
        return (day - payload.period.start_date).days // 7

    def can_work(emp: Employee, day: date, start: str, end: str, role: str, weekly_hours: float) -> bool:
        if role not in emp.roles:
            return False
        if (emp.id, day.isoformat()) in unavailability:
            return False
        if not has_time_window(getattr(emp.availability, day_key(day)), start, end):
            return False
        return weekly_hours + duration_hours(start, end) <= emp.max_hours_per_week

    def select_employee(day: date, start: str, end: str, role_options: List[str]) -> Employee | None:
        w = week_index(day)
        candidates = []
        for emp in employees.values():
            matching_role = next((r for r in role_options if r in emp.roles), None)
            if not matching_role:
                continue
            if can_work(emp, day, start, end, matching_role, week_hours[emp.id][w]):
                candidates.append((
                    tier_rank[emp.priority_tier],
                    week_hours[emp.id][w],
                    1 if worked_day[(emp.id, day.isoformat())] else 0,
                    emp.id,
                ))
        if not candidates:
            return None
        selected_id = sorted(candidates)[0][3]
        return employees[selected_id]

    for day in days:
        if not greystones_open(day, payload.season_rules):
            continue

        w = week_index(day)
        date_str = day.isoformat()

        g_staff = (
            payload.coverage.greystones_weekend_staff
            if is_weekend(day)
            else payload.coverage.greystones_weekday_staff
        )

        manager_scheduled = False
        leaders_today = 0

        captain = select_employee(
            day,
            payload.hours.greystones.start,
            payload.hours.greystones.end,
            ["Boat Captain"],
        )
        if captain:
            assignments.append(
                {
                    "date": date_str,
                    "location": "Boat",
                    "start": payload.hours.greystones.start,
                    "end": payload.hours.greystones.end,
                    "employee_id": captain.id,
                    "role": "Boat Captain",
                }
            )
            hrs = duration_hours(payload.hours.greystones.start, payload.hours.greystones.end)
            week_hours[captain.id][w] += hrs
            if not worked_day[(captain.id, date_str)]:
                week_days[captain.id][w] += 1
                if is_weekend(day):
                    weekend_days[captain.id] += 1
            worked_day[(captain.id, date_str)] = True
            locations[captain.id]["Boat"] += 1
        else:
            violations.append(
                {
                    "date": date_str,
                    "type": "role_missing",
                    "detail": "No Boat Captain available for open day.",
                }
            )

        for _ in range(g_staff):
            worker = select_employee(
                day,
                payload.hours.greystones.start,
                payload.hours.greystones.end,
                ["Store Manager", "Team Leader", "Store Clerk"],
            )
            if not worker:
                violations.append(
                    {
                        "date": date_str,
                        "type": "coverage_gap",
                        "detail": "Unable to fill Greystones staffing demand.",
                    }
                )
                continue
            role = (
                "Store Manager"
                if "Store Manager" in worker.roles and not manager_scheduled
                else "Team Leader"
                if "Team Leader" in worker.roles and leaders_today < payload.leadership_rules.weekend_team_leaders_if_manager_off
                else "Store Clerk"
                if "Store Clerk" in worker.roles
                else "Team Leader"
            )
            assignments.append(
                {
                    "date": date_str,
                    "location": "Greystones",
                    "start": payload.hours.greystones.start,
                    "end": payload.hours.greystones.end,
                    "employee_id": worker.id,
                    "role": role,
                }
            )
            hrs = duration_hours(payload.hours.greystones.start, payload.hours.greystones.end)
            week_hours[worker.id][w] += hrs
            if not worked_day[(worker.id, date_str)]:
                week_days[worker.id][w] += 1
                if is_weekend(day):
                    weekend_days[worker.id] += 1
            worked_day[(worker.id, date_str)] = True
            locations[worker.id]["Greystones"] += 1
            if role == "Store Manager":
                manager_scheduled = True
            if role == "Team Leader":
                leaders_today += 1

        if leaders_today < payload.leadership_rules.min_team_leaders_every_open_day:
            violations.append(
                {
                    "date": date_str,
                    "type": "leader_gap",
                    "detail": "Minimum team leader coverage not met for Greystones.",
                }
            )

        if is_weekend(day) and not manager_scheduled and leaders_today < payload.leadership_rules.weekend_team_leaders_if_manager_off:
            violations.append(
                {
                    "date": date_str,
                    "type": "leader_gap",
                    "detail": "Weekend day without manager has fewer than required team leaders.",
                }
            )

        if beach_shop_open(day, payload.season_rules):
            for _ in range(payload.coverage.beach_shop_staff):
                beach_worker = select_employee(
                    day,
                    payload.hours.beach_shop.start,
                    payload.hours.beach_shop.end,
                    ["Store Manager", "Team Leader", "Store Clerk"],
                )
                if not beach_worker:
                    violations.append(
                        {
                            "date": date_str,
                            "type": "beach_shop_gap",
                            "detail": "Unable to fill Beach Shop staffing demand.",
                        }
                    )
                    continue
                role = (
                    "Store Manager"
                    if "Store Manager" in beach_worker.roles and not manager_scheduled
                    else "Team Leader"
                    if "Team Leader" in beach_worker.roles
                    else "Store Clerk"
                )
                assignments.append(
                    {
                        "date": date_str,
                        "location": "Beach Shop",
                        "start": payload.hours.beach_shop.start,
                        "end": payload.hours.beach_shop.end,
                        "employee_id": beach_worker.id,
                        "role": role,
                    }
                )
                hrs = duration_hours(payload.hours.beach_shop.start, payload.hours.beach_shop.end)
                week_hours[beach_worker.id][w] += hrs
                if not worked_day[(beach_worker.id, date_str)]:
                    week_days[beach_worker.id][w] += 1
                    if is_weekend(day):
                        weekend_days[beach_worker.id] += 1
                worked_day[(beach_worker.id, date_str)] = True
                locations[beach_worker.id]["Beach Shop"] += 1
                if role == "Store Manager":
                    manager_scheduled = True
                if role == "Team Leader":
                    leaders_today += 1

    # Manager consecutive days off check (Mon-Sun calendar weeks)
    managers = [e for e in employees.values() if "Store Manager" in e.roles]
    if payload.leadership_rules.manager_two_consecutive_days_off_per_week and managers:
        for manager in managers:
            start = payload.period.start_date
            end = payload.period.start_date + timedelta(days=payload.period.weeks * 7 - 1)
            cursor = start - timedelta(days=start.weekday())
            while cursor <= end:
                week_days_dates = [cursor + timedelta(days=i) for i in range(7)]
                open_in_scope = [d for d in week_days_dates if start <= d <= end and greystones_open(d, payload.season_rules)]
                if not open_in_scope:
                    cursor += timedelta(days=7)
                    continue
                works = [worked_day[(manager.id, d.isoformat())] for d in week_days_dates]
                has_pair = any((not works[i] and not works[i + 1]) for i in range(6))
                if not has_pair:
                    violations.append(
                        {
                            "date": cursor.isoformat(),
                            "type": "manager_consecutive_days_off",
                            "detail": f"Manager {manager.name} lacks two consecutive days off in calendar week starting {cursor.isoformat()}.",
                        }
                    )
                cursor += timedelta(days=7)

    totals_by_employee = {}
    for emp_id, emp in employees.items():
        record = {
            "week1_hours": round(week_hours[emp_id][0], 2) if payload.period.weeks >= 1 else 0,
            "week2_hours": round(week_hours[emp_id][1], 2) if payload.period.weeks >= 2 else 0,
            "week1_days": week_days[emp_id][0] if payload.period.weeks >= 1 else 0,
            "week2_days": week_days[emp_id][1] if payload.period.weeks >= 2 else 0,
            "weekend_days": weekend_days[emp_id],
            "locations": locations[emp_id],
        }

        for w in range(payload.period.weeks):
            if week_hours[emp_id][w] < emp.min_hours_per_week:
                violations.append(
                    {
                        "date": (payload.period.start_date + timedelta(days=7 * w)).isoformat(),
                        "type": "max_hours",
                        "detail": f"Soft target not met: {emp.name} below min hours in week {w + 1}.",
                    }
                )
        totals_by_employee[emp_id] = record

    assignments.sort(key=lambda a: (a["date"], a["location"], a["start"], a["employee_id"]))
    violations.sort(key=lambda v: (v["date"], v["type"], v["detail"]))

    return {
        "assignments": assignments,
        "totals_by_employee": totals_by_employee,
        "violations": violations,
    }


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    sample_json = json.dumps(SAMPLE_PAYLOAD, indent=2)
    return f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>FOBE Scheduler Prototype</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    textarea {{ width: 100%; min-height: 420px; font-family: monospace; }}
    button {{ margin: 6px 6px 12px 0; padding: 8px 12px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border: 1px solid #ccc; padding: 6px; text-align: left; }}
    .error {{ color: #b00020; }}
  </style>
</head>
<body>
  <h1>FOBE Scheduler Prototype</h1>
  <p>Paste/edit payload JSON, then generate a deterministic 2-week draft schedule.</p>
  <form id=\"generator\">
    <textarea id=\"payload\">{sample_json}</textarea><br>
    <button type=\"submit\">Generate 2-week schedule</button>
    <button type=\"button\" id=\"downloadJson\">Download JSON</button>
    <button type=\"button\" id=\"downloadCsv\">Download CSV</button>
  </form>
  <div id=\"errors\" class=\"error\"></div>
  <div id=\"results\"></div>

<script>
let latestResult = null;

function render(result) {{
  const assignmentsRows = result.assignments.map(a => `
    <tr><td>${{a.date}}</td><td>${{a.location}}</td><td>${{a.start}}-${{a.end}}</td><td>${{a.employee_id}}</td><td>${{a.role}}</td></tr>`).join('');

  const violationsRows = result.violations.map(v => `
    <tr><td>${{v.date}}</td><td>${{v.type}}</td><td>${{v.detail}}</td></tr>`).join('');

  document.getElementById('results').innerHTML = `
    <h2>Schedule</h2>
    <table>
      <thead><tr><th>Date</th><th>Location</th><th>Time</th><th>Employee</th><th>Role</th></tr></thead>
      <tbody>${{assignmentsRows || '<tr><td colspan="5">No assignments</td></tr>'}}</tbody>
    </table>
    <h2>Violations</h2>
    <table>
      <thead><tr><th>Date</th><th>Type</th><th>Detail</th></tr></thead>
      <tbody>${{violationsRows || '<tr><td colspan="3">No violations</td></tr>'}}</tbody>
    </table>
  `;
}}

document.getElementById('generator').addEventListener('submit', async (e) => {{
  e.preventDefault();
  document.getElementById('errors').textContent = '';
  try {{
    const payload = JSON.parse(document.getElementById('payload').value);
    const res = await fetch('/generate', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }});
    if (!res.ok) {{
      document.getElementById('errors').textContent = await res.text();
      return;
    }}
    latestResult = await res.json();
    render(latestResult);
  }} catch (err) {{
    document.getElementById('errors').textContent = 'Invalid JSON or request failed.';
  }}
}});

document.getElementById('downloadJson').addEventListener('click', () => {{
  window.location.href = '/export/json';
}});

document.getElementById('downloadCsv').addEventListener('click', () => {{
  window.location.href = '/export/csv';
}});
</script>
</body>
</html>
"""


@app.post("/generate")
def generate(payload: GenerateRequest):
    global LAST_RESULT
    result = build_schedule(payload)
    LAST_RESULT = result
    return JSONResponse(result)


@app.get("/export/json")
def export_json():
    if LAST_RESULT is None:
        raise HTTPException(status_code=400, detail="No generated schedule available yet.")
    return JSONResponse(LAST_RESULT)


@app.get("/export/csv")
def export_csv():
    if LAST_RESULT is None:
        raise HTTPException(status_code=400, detail="No generated schedule available yet.")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "location", "start", "end", "employee_id", "role"])
    for row in LAST_RESULT["assignments"]:
        writer.writerow([row["date"], row["location"], row["start"], row["end"], row["employee_id"], row["role"]])

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    headers = {"Content-Disposition": 'attachment; filename="fobe_schedule.csv"'}
    return StreamingResponse(mem, media_type="text/csv", headers=headers)
