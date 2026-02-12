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


def _sample_payload_dict() -> dict:
    return {
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
        }


def _sample_payload() -> str:
    return json.dumps(_sample_payload_dict(), indent=2)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    payload = json.dumps(_sample_payload_dict())
    export_links = ""
    if _LAST_RESULT is not None:
        export_links = '<p class="text-sm text-slate-600 mt-2">Downloads: <a class="text-blue-700 underline" href="/export/json">JSON</a> · <a class="text-blue-700 underline" href="/export/csv">CSV</a></p>'
    return f"""
<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>FOBE Scheduler</title></head>
<body style=\"font-family: Inter, system-ui, sans-serif; margin: 0; background: #f1f5f9; color: #0f172a;\">
<div style=\"max-width: 1180px; margin: 0 auto; padding: 1.5rem;\">
  <header style=\"display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;\">
    <div>
      <h1 style=\"margin:0; font-size:1.6rem;\">FOBE Schedule Builder</h1>
      <p style=\"margin:0.4rem 0 0; color:#475569;\">Plan your staffing schedule with forms, buttons, and guided controls — no JSON required.</p>
    </div>
    <div>
      <button onclick=\"loadSampleData()\" style=\"border:none; background:#334155; color:#fff; border-radius:0.5rem; padding:0.65rem 0.9rem; cursor:pointer;\">Load Example Data</button>
      <button onclick=\"runGenerate()\" style=\"border:none; background:#0f172a; color:#fff; border-radius:0.5rem; padding:0.65rem 0.9rem; margin-left:0.5rem; cursor:pointer;\">Generate Schedule</button>
    </div>
  </header>
  <p id=\"feedback\" style=\"display:none; margin:0 0 1rem; padding:0.7rem; border-radius:0.5rem;\"></p>
  <section style=\"display:grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap:1rem; margin-bottom:1rem;\">
    <div style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06);\">
      <h2 style=\"margin:0 0 0.75rem; font-size:1.1rem;\">Schedule Period</h2>
      <label style=\"display:block; margin-bottom:0.6rem;\">Start Date <input id=\"start_date\" type=\"date\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Weeks <select id=\"weeks\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"><option value=\"1\">1 week</option><option value=\"2\" selected>2 weeks</option><option value=\"3\">3 weeks</option><option value=\"4\">4 weeks</option></select></label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Manager weekends already worked this month <input id=\"manager_weekends\" type=\"number\" min=\"0\" value=\"0\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
    </div>
    <div style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06);\">
      <h2 style=\"margin:0 0 0.75rem; font-size:1.1rem;\">Coverage Targets</h2>
      <label style=\"display:block; margin-bottom:0.6rem;\">Greystones weekday staff <input id=\"g_weekday\" type=\"number\" min=\"1\" value=\"3\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Greystones weekend staff <input id=\"g_weekend\" type=\"number\" min=\"1\" value=\"4\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Beach Shop staff <input id=\"beach_staff\" type=\"number\" min=\"0\" value=\"2\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
    </div>
    <div style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06);\">
      <h2 style=\"margin:0 0 0.75rem; font-size:1.1rem;\">Leadership Rules</h2>
      <label style=\"display:block; margin-bottom:0.6rem;\">Min Team Leaders per open day <input id=\"min_leaders\" type=\"number\" min=\"0\" value=\"1\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Weekend Team Leaders if manager off <input id=\"weekend_leaders\" type=\"number\" min=\"0\" value=\"2\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
      <label style=\"display:block; margin-bottom:0.6rem;\"><input id=\"manager_two_off\" type=\"checkbox\" checked> Require manager to have two consecutive days off per week</label>
      <label style=\"display:block; margin-bottom:0.6rem;\">Manager min weekends per month <input id=\"manager_min_weekends\" type=\"number\" min=\"0\" value=\"2\" style=\"width:100%; padding:0.5rem; margin-top:0.25rem;\"></label>
    </div>
  </section>
  <section style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06); margin-bottom:1rem;\">
    <div style=\"display:flex; justify-content:space-between; align-items:center;\"><h2 style=\"margin:0; font-size:1.1rem;\">Employees</h2><button onclick=\"addEmployeeRow()\" style=\"border:none; background:#1d4ed8; color:#fff; border-radius:0.5rem; padding:0.45rem 0.8rem; cursor:pointer;\">Add Employee</button></div>
    <p style=\"color:#475569; font-size:0.9rem;\">Choose roles and staffing limits. Availability defaults to all days, full shift.</p>
    <div style=\"overflow:auto;\"><table style=\"width:100%; border-collapse:collapse; font-size:0.92rem;\"><thead><tr style=\"background:#f8fafc;\"><th style=\"text-align:left; padding:0.5rem;\">ID</th><th style=\"text-align:left; padding:0.5rem;\">Name</th><th style=\"text-align:left; padding:0.5rem;\">Roles</th><th style=\"text-align:left; padding:0.5rem;\">Min hrs/wk</th><th style=\"text-align:left; padding:0.5rem;\">Max hrs/wk</th><th style=\"text-align:left; padding:0.5rem;\">Priority</th><th></th></tr></thead><tbody id=\"employee_rows\"></tbody></table></div>
  </section>
  <section style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06); margin-bottom:1rem;\">
    <div style=\"display:flex; justify-content:space-between; align-items:center;\"><h2 style=\"margin:0; font-size:1.1rem;\">Time Off</h2><button onclick=\"addTimeOffRow()\" style=\"border:none; background:#0891b2; color:#fff; border-radius:0.5rem; padding:0.45rem 0.8rem; cursor:pointer;\">Add Time Off</button></div>
    <div style=\"overflow:auto; margin-top:0.5rem;\"><table style=\"width:100%; border-collapse:collapse; font-size:0.92rem;\"><thead><tr style=\"background:#f8fafc;\"><th style=\"text-align:left; padding:0.5rem;\">Employee</th><th style=\"text-align:left; padding:0.5rem;\">Date</th><th style=\"text-align:left; padding:0.5rem;\">Reason</th><th></th></tr></thead><tbody id=\"time_off_rows\"></tbody></table></div>
  </section>
  <section style=\"background:#fff; border-radius:0.75rem; padding:1rem; box-shadow:0 1px 4px rgba(0,0,0,0.06); margin-bottom:2rem;\">
    <h2 style=\"margin-top:0; font-size:1.1rem;\">Results</h2>
    {export_links}
    <div id=\"result\" style=\"font-size:0.95rem;\"><p style=\"color:#64748b;\">Generate a run to see assignments and rule violations.</p></div>
  </section>
</div>
<script>
const ROLE_OPTIONS = ['Store Clerk','Team Leader','Store Manager','Boat Captain'];
const DAY_KEYS = ['mon','tue','wed','thu','fri','sat','sun'];
const SAMPLE_PAYLOAD = {payload};

function showMessage(msg, kind='info') {{
  const el = document.getElementById('feedback');
  const styles = kind === 'error'
    ? 'background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;'
    : 'background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd;';
  el.style = `margin:0 0 1rem; padding:0.7rem; border-radius:0.5rem; ${{styles}}`;
  el.textContent = msg;
  el.style.display = 'block';
}}

function clearMessage() {{ document.getElementById('feedback').style.display = 'none'; }}

function employeeRowHtml(emp={{}}) {{
  const roles = new Set(emp.roles || ['Store Clerk']);
  const roleChecks = ROLE_OPTIONS.map(r => `<label style="display:block;"><input type="checkbox" class="emp-role" value="${{r}}" ${{roles.has(r) ? 'checked' : ''}}> ${{r}}</label>`).join('');
  return `<tr style="border-top:1px solid #e2e8f0;"><td style="padding:0.45rem;"><input class="emp-id" value="${{emp.id || ''}}" placeholder="e1" style="width:90px; padding:0.35rem;"></td><td style="padding:0.45rem;"><input class="emp-name" value="${{emp.name || ''}}" placeholder="Employee name" style="width:100%; min-width:140px; padding:0.35rem;"></td><td style="padding:0.45rem; min-width:160px;">${{roleChecks}}</td><td style="padding:0.45rem;"><input type="number" min="0" class="emp-min" value="${{emp.min_hours_per_week ?? 16}}" style="width:85px; padding:0.35rem;"></td><td style="padding:0.45rem;"><input type="number" min="1" class="emp-max" value="${{emp.max_hours_per_week ?? 40}}" style="width:85px; padding:0.35rem;"></td><td style="padding:0.45rem;"><select class="emp-priority" style="padding:0.35rem;"><option value="A" ${{emp.priority_tier === 'A' ? 'selected' : ''}}>A</option><option value="B" ${{emp.priority_tier !== 'A' && emp.priority_tier !== 'C' ? 'selected' : ''}}>B</option><option value="C" ${{emp.priority_tier === 'C' ? 'selected' : ''}}>C</option></select></td><td style="padding:0.45rem;"><button onclick="this.closest('tr').remove(); refreshEmployeeDropdowns();" style="border:none; background:#ef4444; color:#fff; border-radius:0.35rem; padding:0.35rem 0.5rem; cursor:pointer;">Remove</button></td></tr>`;
}}

function addEmployeeRow(emp={{}}) {{
  document.getElementById('employee_rows').insertAdjacentHTML('beforeend', employeeRowHtml(emp));
  refreshEmployeeDropdowns();
}}

function timeOffRowHtml(row={{}}) {{
  return `<tr style="border-top:1px solid #e2e8f0;"><td style="padding:0.45rem;"><select class="to-employee" style="padding:0.35rem; min-width:120px;"></select></td><td style="padding:0.45rem;"><input type="date" class="to-date" value="${{row.date || ''}}" style="padding:0.35rem;"></td><td style="padding:0.45rem;"><input class="to-reason" value="${{row.reason || ''}}" placeholder="Vacation, appointment..." style="width:100%; min-width:160px; padding:0.35rem;"></td><td style="padding:0.45rem;"><button onclick="this.closest('tr').remove();" style="border:none; background:#ef4444; color:#fff; border-radius:0.35rem; padding:0.35rem 0.5rem; cursor:pointer;">Remove</button></td></tr>`;
}}

function addTimeOffRow(row={{}}) {{
  const tbody = document.getElementById('time_off_rows');
  tbody.insertAdjacentHTML('beforeend', timeOffRowHtml(row));
  const newRow = tbody.lastElementChild;
  refreshEmployeeDropdowns();
  if (row.employee_id) {{
    const select = newRow.querySelector('.to-employee');
    select.value = row.employee_id;
  }}
}}

function refreshEmployeeDropdowns() {{
  const employees = [...document.querySelectorAll('#employee_rows tr')].map((tr, idx) => {{
    const id = tr.querySelector('.emp-id').value.trim() || `e${{idx + 1}}`;
    const name = tr.querySelector('.emp-name').value.trim() || id;
    return {{ id, name }};
  }});
  const options = employees.map(e => `<option value="${{e.id}}">${{e.name}} (${{e.id}})</option>`).join('');
  document.querySelectorAll('.to-employee').forEach(sel => {{
    const current = sel.value;
    sel.innerHTML = options;
    if (current) sel.value = current;
  }});
}}

function collectPayload() {{
  const employees = [...document.querySelectorAll('#employee_rows tr')].map((tr, idx) => {{
    const id = tr.querySelector('.emp-id').value.trim() || `e${{idx + 1}}`;
    const name = tr.querySelector('.emp-name').value.trim() || `Employee ${{idx + 1}}`;
    const roles = [...tr.querySelectorAll('.emp-role:checked')].map(i => i.value);
    return {{
      id,
      name,
      roles: roles.length ? roles : ['Store Clerk'],
      min_hours_per_week: Number(tr.querySelector('.emp-min').value || 0),
      max_hours_per_week: Number(tr.querySelector('.emp-max').value || 40),
      priority_tier: tr.querySelector('.emp-priority').value,
      availability: Object.fromEntries(DAY_KEYS.map(day => [day, ['08:30-17:30']]))
    }};
  }});

  const unavailability = [...document.querySelectorAll('#time_off_rows tr')]
    .map(tr => ({{
      employee_id: tr.querySelector('.to-employee').value,
      date: tr.querySelector('.to-date').value,
      reason: tr.querySelector('.to-reason').value.trim()
    }}))
    .filter(row => row.employee_id && row.date);

  if (!employees.length) throw new Error('Add at least one employee before generating.');

  return {{
    period: {{ start_date: document.getElementById('start_date').value, weeks: Number(document.getElementById('weeks').value) }},
    season_rules: {{
      victoria_day: '2025-05-19', june_30: '2025-06-30', labour_day: '2025-09-01', oct_31: '2025-10-31'
    }},
    hours: {{
      greystones: {{ start: '08:30', end: '17:30' }},
      beach_shop: {{ start: '12:00', end: '16:00' }}
    }},
    coverage: {{
      greystones_weekday_staff: Number(document.getElementById('g_weekday').value),
      greystones_weekend_staff: Number(document.getElementById('g_weekend').value),
      beach_shop_staff: Number(document.getElementById('beach_staff').value)
    }},
    leadership_rules: {{
      min_team_leaders_every_open_day: Number(document.getElementById('min_leaders').value),
      weekend_team_leaders_if_manager_off: Number(document.getElementById('weekend_leaders').value),
      manager_two_consecutive_days_off_per_week: document.getElementById('manager_two_off').checked,
      manager_min_weekends_per_month: Number(document.getElementById('manager_min_weekends').value)
    }},
    employees,
    unavailability,
    history: {{ manager_weekends_worked_this_month: Number(document.getElementById('manager_weekends').value) }}
  }};
}}

function fillForm(data) {{
  document.getElementById('start_date').value = data.period.start_date;
  document.getElementById('weeks').value = String(data.period.weeks || 2);
  document.getElementById('manager_weekends').value = data.history?.manager_weekends_worked_this_month ?? 0;
  document.getElementById('g_weekday').value = data.coverage.greystones_weekday_staff;
  document.getElementById('g_weekend').value = data.coverage.greystones_weekend_staff;
  document.getElementById('beach_staff').value = data.coverage.beach_shop_staff;
  document.getElementById('min_leaders').value = data.leadership_rules.min_team_leaders_every_open_day;
  document.getElementById('weekend_leaders').value = data.leadership_rules.weekend_team_leaders_if_manager_off;
  document.getElementById('manager_two_off').checked = data.leadership_rules.manager_two_consecutive_days_off_per_week;
  document.getElementById('manager_min_weekends').value = data.leadership_rules.manager_min_weekends_per_month;
  document.getElementById('employee_rows').innerHTML = '';
  data.employees.forEach(addEmployeeRow);
  document.getElementById('time_off_rows').innerHTML = '';
  (data.unavailability || []).forEach(addTimeOffRow);
  if (!data.unavailability?.length) addTimeOffRow();
  refreshEmployeeDropdowns();
  clearMessage();
}}

function loadSampleData() {{
  fillForm(SAMPLE_PAYLOAD);
  showMessage('Sample data loaded. Review and click Generate Schedule.');
}}

async function runGenerate() {{
  clearMessage();
  let data;
  try {{ data = collectPayload(); }} catch (err) {{ showMessage(err.message, 'error'); return; }}
  if (!data.period.start_date) {{ showMessage('Choose a start date before generating.', 'error'); return; }}
  const res = await fetch('/generate', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(data) }});
  const json = await res.json();
  if (!res.ok) {{ showMessage('Could not generate schedule. Please review your entries.', 'error'); document.getElementById('result').innerText = JSON.stringify(json, null, 2); return; }}
  const rows = json.assignments.map(a => `<tr style="border-top:1px solid #e2e8f0;"><td style="padding:0.45rem;">${{a.date}}</td><td style="padding:0.45rem;">${{a.location}}</td><td style="padding:0.45rem;">${{a.start}}-${{a.end}}</td><td style="padding:0.45rem;">${{a.employee_id}}</td><td style="padding:0.45rem;">${{a.role}}</td></tr>`).join('');
  const viol = json.violations.length
    ? `<ul style="padding-left:1.2rem;">${{json.violations.map(v => `<li><strong>${{v.date}}</strong> · ${{v.type}} — ${{v.detail}}</li>`).join('')}}</ul>`
    : '<p style="color:#16a34a;">No violations.</p>';
  document.getElementById('result').innerHTML = `<p style="margin:0 0 0.7rem;">Downloads: <a href=\"/export/json\" class=\"text-blue-700\">JSON</a> · <a href=\"/export/csv\" class=\"text-blue-700\">CSV</a></p><div style="display:grid; grid-template-columns: 2fr 1fr; gap:1rem;"><div><table style="width:100%; border-collapse:collapse;"><thead><tr style="background:#f8fafc;"><th style="text-align:left;padding:0.45rem;">Date</th><th style="text-align:left;padding:0.45rem;">Location</th><th style="text-align:left;padding:0.45rem;">Shift</th><th style="text-align:left;padding:0.45rem;">Employee</th><th style="text-align:left;padding:0.45rem;">Role</th></tr></thead><tbody>${{rows}}</tbody></table></div><div><h3 style="margin:0 0 0.5rem;">Rule Checks</h3>${{viol}}</div></div>`;
  showMessage(`Generated ${{json.assignments.length}} assignments successfully.`);
}}

fillForm(SAMPLE_PAYLOAD);
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
