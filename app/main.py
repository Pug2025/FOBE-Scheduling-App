from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Literal, Tuple

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="FOBE Scheduler Prototype")


DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ROLE_PRIORITY = ["Store Manager", "Team Leader", "Boat Captain", "Store Clerk"]
TIER_ORDER = {"A": 0, "B": 1, "C": 2}


class Period(BaseModel):
    start_date: date
    weeks: int = 2


class SeasonRules(BaseModel):
    victoria_day: date
    june_30: date
    labour_day: date
    oct_31: date


class LocationHours(BaseModel):
    start: str
    end: str


class Hours(BaseModel):
    greystones: LocationHours
    beach_shop: LocationHours


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
    roles: List[Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]]
    min_hours_per_week: float
    max_hours_per_week: float
    priority_tier: Literal["A", "B", "C"]
    availability: Dict[str, List[str]]


class UnavailabilityEntry(BaseModel):
    employee_id: str
    date: date
    reason: str


class History(BaseModel):
    manager_weekends_worked_this_month: int


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
    date: date
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    role: Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]


class EmployeeTotals(BaseModel):
    week1_hours: float = 0
    week2_hours: float = 0
    week1_days: int = 0
    week2_days: int = 0
    weekend_days: int = 0
    locations: Dict[str, int] = Field(default_factory=lambda: {"Greystones": 0, "Beach Shop": 0, "Boat": 0})


class Violation(BaseModel):
    date: date
    type: Literal[
        "coverage_gap",
        "leader_gap",
        "manager_consecutive_days_off",
        "max_hours",
        "role_missing",
        "beach_shop_gap",
    ]
    detail: str


class GenerateResponse(BaseModel):
    assignments: List[Assignment]
    totals_by_employee: Dict[str, EmployeeTotals]
    violations: List[Violation]


def parse_time(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def shift_hours(start: str, end: str) -> float:
    return round((parse_time(end) - parse_time(start)) / 60, 2)


def daterange(start: date, days: int):
    for i in range(days):
        yield start + timedelta(days=i)


def weekday_key(d: date) -> str:
    return DAY_KEYS[d.weekday()]


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def in_range(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def greystones_open(d: date, req: GenerateRequest) -> bool:
    july_1 = req.season_rules.june_30 + timedelta(days=1)
    if in_range(d, req.season_rules.victoria_day, req.season_rules.june_30):
        return is_weekend(d)
    if in_range(d, july_1, req.season_rules.labour_day):
        return True
    if in_range(d, req.season_rules.labour_day + timedelta(days=1), req.season_rules.oct_31):
        return is_weekend(d)
    return False


def beach_shop_open(d: date, req: GenerateRequest) -> bool:
    july_1 = req.season_rules.june_30 + timedelta(days=1)
    return in_range(d, july_1, req.season_rules.labour_day) and is_weekend(d)


def employee_available(e: Employee, d: date, start: str, end: str) -> bool:
    windows = e.availability.get(weekday_key(d), [])
    needed_s = parse_time(start)
    needed_e = parse_time(end)
    for window in windows:
        s, e_str = window.split("-")
        if parse_time(s) <= needed_s and parse_time(e_str) >= needed_e:
            return True
    return False


def make_assignment(d: date, location: str, start: str, end: str, employee_id: str, role: str) -> Assignment:
    return Assignment(date=d, location=location, start=start, end=end, employee_id=employee_id, role=role)


def choose_candidate(
    candidates: List[Employee],
    d: date,
    week_index: int,
    weekly_hours: Dict[Tuple[str, int], float],
    req: GenerateRequest,
    role_preference: List[str] | None = None,
) -> Employee | None:
    def score(emp: Employee):
        current_hours = weekly_hours[(emp.id, week_index)]
        min_target = emp.min_hours_per_week
        under_min = 0 if current_hours >= min_target else min_target - current_hours
        role_rank = min([role_preference.index(r) for r in emp.roles if role_preference and r in role_preference], default=99)
        return (
            -under_min,
            TIER_ORDER[emp.priority_tier],
            current_hours,
            role_rank,
            emp.id,
        )

    ordered = sorted(candidates, key=score)
    return ordered[0] if ordered else None


def generate_schedule(req: GenerateRequest) -> GenerateResponse:
    start = req.period.start_date
    total_days = req.period.weeks * 7
    unavailable = {(u.employee_id, u.date) for u in req.unavailability}

    employee_map = {e.id: e for e in sorted(req.employees, key=lambda x: x.id)}
    assignments: List[Assignment] = []
    violations: List[Violation] = []

    weekly_hours: Dict[Tuple[str, int], float] = defaultdict(float)
    day_assignments: Dict[Tuple[date, str], List[str]] = defaultdict(list)

    def candidate_pool(d: date, start_t: str, end_t: str, role: str | None = None):
        pool = []
        week_index = ((d - start).days // 7) + 1
        for e in employee_map.values():
            if (e.id, d) in unavailable:
                continue
            if role and role not in e.roles:
                continue
            if not employee_available(e, d, start_t, end_t):
                continue
            if e.id in day_assignments[(d, "all")]:
                continue
            shift_len = shift_hours(start_t, end_t)
            if weekly_hours[(e.id, week_index)] + shift_len > e.max_hours_per_week:
                continue
            pool.append(e)
        return pool

    for d in daterange(start, total_days):
        if not greystones_open(d, req):
            continue

        week_index = ((d - start).days // 7) + 1
        g_start, g_end = req.hours.greystones.start, req.hours.greystones.end
        g_need = req.coverage.greystones_weekend_staff if is_weekend(d) else req.coverage.greystones_weekday_staff

        manager_scheduled = False
        team_leader_count = 0

        # Boat captain (hard)
        boat_pool = candidate_pool(d, g_start, g_end, "Boat Captain")
        boat_pick = choose_candidate(boat_pool, d, week_index, weekly_hours, req, ["Boat Captain"])
        if boat_pick:
            assignments.append(make_assignment(d, "Boat", g_start, g_end, boat_pick.id, "Boat Captain"))
            weekly_hours[(boat_pick.id, week_index)] += shift_hours(g_start, g_end)
            day_assignments[(d, "all")].append(boat_pick.id)
            day_assignments[(d, "Boat")].append(boat_pick.id)
        else:
            violations.append(Violation(date=d, type="role_missing", detail="No Boat Captain available"))

        # Greystones coverage
        for _ in range(g_need):
            pool = candidate_pool(d, g_start, g_end)
            pick = choose_candidate(pool, d, week_index, weekly_hours, req, ROLE_PRIORITY)
            if not pick:
                violations.append(Violation(date=d, type="coverage_gap", detail="Greystones staffing below required level"))
                break

            role = "Store Clerk"
            if not manager_scheduled and "Store Manager" in pick.roles:
                role = "Store Manager"
                manager_scheduled = True
            elif "Team Leader" in pick.roles:
                role = "Team Leader"

            if role == "Store Manager":
                manager_scheduled = True
            if role == "Team Leader":
                team_leader_count += 1

            assignments.append(make_assignment(d, "Greystones", g_start, g_end, pick.id, role))
            weekly_hours[(pick.id, week_index)] += shift_hours(g_start, g_end)
            day_assignments[(d, "all")].append(pick.id)
            day_assignments[(d, "Greystones")].append(pick.id)

        if team_leader_count < req.leadership_rules.min_team_leaders_every_open_day:
            # try to upgrade one clerk to leader if leader exists on shift employee roles
            for a in assignments:
                if a.date == d and a.location == "Greystones" and a.role == "Store Clerk":
                    e = employee_map[a.employee_id]
                    if "Team Leader" in e.roles:
                        a.role = "Team Leader"
                        team_leader_count += 1
                        break
        if team_leader_count < req.leadership_rules.min_team_leaders_every_open_day:
            violations.append(Violation(date=d, type="leader_gap", detail="Minimum team leader requirement not met"))

        if is_weekend(d) and not manager_scheduled and team_leader_count < req.leadership_rules.weekend_team_leaders_if_manager_off:
            violations.append(
                Violation(
                    date=d,
                    type="leader_gap",
                    detail="Weekend requires additional Team Leader coverage when manager is off",
                )
            )

        # Beach shop coverage
        if beach_shop_open(d, req):
            b_start, b_end = req.hours.beach_shop.start, req.hours.beach_shop.end
            for _ in range(req.coverage.beach_shop_staff):
                pool = candidate_pool(d, b_start, b_end)
                pick = choose_candidate(pool, d, week_index, weekly_hours, req, ROLE_PRIORITY)
                if not pick:
                    violations.append(Violation(date=d, type="beach_shop_gap", detail="Beach Shop staffing below required level"))
                    break
                role = "Store Clerk"
                if "Store Manager" in pick.roles and not manager_scheduled:
                    role = "Store Manager"
                    manager_scheduled = True
                elif "Team Leader" in pick.roles:
                    role = "Team Leader"
                assignments.append(make_assignment(d, "Beach Shop", b_start, b_end, pick.id, role))
                weekly_hours[(pick.id, week_index)] += shift_hours(b_start, b_end)
                day_assignments[(d, "all")].append(pick.id)
                day_assignments[(d, "Beach Shop")].append(pick.id)

    # manager consecutive day off checks (Mon-Sun windows)
    if req.leadership_rules.manager_two_consecutive_days_off_per_week:
        manager_ids = [e.id for e in employee_map.values() if "Store Manager" in e.roles]
        for w in range(req.period.weeks):
            week_start = start + timedelta(days=w * 7)
            week_days = [week_start + timedelta(days=i) for i in range(7)]
            for manager_id in manager_ids:
                worked = {a.date for a in assignments if a.employee_id == manager_id}
                found_pair = any(week_days[i] not in worked and week_days[i + 1] not in worked for i in range(6))
                if not found_pair:
                    violations.append(
                        Violation(
                            date=week_start,
                            type="manager_consecutive_days_off",
                            detail=f"Manager {manager_id} has no consecutive days off in week {w+1}",
                        )
                    )

    totals: Dict[str, EmployeeTotals] = {eid: EmployeeTotals() for eid in employee_map.keys()}
    for a in assignments:
        week_index = ((a.date - start).days // 7) + 1
        h = shift_hours(a.start, a.end)
        if week_index == 1:
            totals[a.employee_id].week1_hours += h
            totals[a.employee_id].week1_days += 1
        elif week_index == 2:
            totals[a.employee_id].week2_hours += h
            totals[a.employee_id].week2_days += 1
        if is_weekend(a.date):
            totals[a.employee_id].weekend_days += 1
        totals[a.employee_id].locations[a.location] += 1

    # deterministic output
    assignments = sorted(assignments, key=lambda a: (a.date, a.location, a.start, a.employee_id))
    violations = sorted(violations, key=lambda v: (v.date, v.type, v.detail))

    return GenerateResponse(assignments=assignments, totals_by_employee=totals, violations=violations)


SAMPLE_PAYLOAD = {
    "period": {"start_date": "2026-07-06", "weeks": 2},
    "season_rules": {
        "victoria_day": "2026-05-18",
        "june_30": "2026-06-30",
        "labour_day": "2026-09-07",
        "oct_31": "2026-10-31",
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
            "name": "Manager Maeve",
            "roles": ["Store Manager", "Team Leader", "Store Clerk"],
            "min_hours_per_week": 24,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
        {
            "id": "e2",
            "name": "Liam",
            "roles": ["Team Leader", "Store Clerk"],
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
        {
            "id": "e3",
            "name": "Niamh",
            "roles": ["Boat Captain", "Store Clerk"],
            "min_hours_per_week": 18,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
        {
            "id": "e4",
            "name": "Conor",
            "roles": ["Team Leader", "Store Clerk"],
            "min_hours_per_week": 16,
            "max_hours_per_week": 35,
            "priority_tier": "B",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
        {
            "id": "e5",
            "name": "Aoife",
            "roles": ["Store Clerk"],
            "min_hours_per_week": 12,
            "max_hours_per_week": 30,
            "priority_tier": "C",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
        {
            "id": "e6",
            "name": "Sean",
            "roles": ["Store Clerk", "Boat Captain"],
            "min_hours_per_week": 12,
            "max_hours_per_week": 30,
            "priority_tier": "C",
            "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
        },
    ],
    "unavailability": [{"employee_id": "e5", "date": "2026-07-12", "reason": "Vacation"}],
    "history": {"manager_weekends_worked_this_month": 0},
}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home():
    sample = GenerateRequest.model_validate(SAMPLE_PAYLOAD).model_dump_json(indent=2)
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>FOBE Scheduler Prototype</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 2rem; max-width: 1200px; }}
      textarea {{ width: 100%; min-height: 320px; font-family: monospace; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
      th, td {{ border: 1px solid #ddd; padding: 6px; font-size: 14px; }}
      th {{ background:#f6f6f6; }}
      .row {{ display:flex; gap:1rem; margin: 1rem 0; }}
      button {{ padding: .5rem 1rem; cursor:pointer; }}
      .violations li {{ color:#9b1c1c; }}
    </style>
  </head>
  <body>
    <h1>FOBE Scheduler Prototype</h1>
    <p>Paste JSON input then generate a deterministic 2-week schedule.</p>
    <textarea id=\"payload\">{sample}</textarea>
    <div class=\"row\">
      <button onclick=\"generate()\">Generate 2-week schedule</button>
      <button onclick=\"downloadJson()\">Download JSON</button>
      <button onclick=\"downloadCsv()\">Download CSV</button>
    </div>
    <div id=\"status\"></div>
    <h2>Assignments</h2>
    <div id=\"table\"></div>
    <h2>Violations</h2>
    <ul id=\"violations\" class=\"violations\"></ul>

    <script>
      let latest = null;
      function esc(v) {{ return String(v).replaceAll('<','&lt;').replaceAll('>','&gt;'); }}
      async function generate() {{
        const status = document.getElementById('status');
        status.textContent = 'Generating...';
        try {{
          const payload = JSON.parse(document.getElementById('payload').value);
          const res = await fetch('/generate', {{
            method:'POST',
            headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify(payload)
          }});
          if (!res.ok) throw new Error(await res.text());
          latest = await res.json();
          render(latest);
          status.textContent = 'Schedule generated.';
        }} catch (e) {{
          status.textContent = 'Error: ' + e.message;
        }}
      }}

      function render(data) {{
        const rows = data.assignments.map(a => `<tr><td>${{esc(a.date)}}</td><td>${{esc(a.location)}}</td><td>${{esc(a.start)}}-${{esc(a.end)}}</td><td>${{esc(a.employee_id)}}</td><td>${{esc(a.role)}}</td></tr>`).join('');
        document.getElementById('table').innerHTML = `<table><thead><tr><th>Date</th><th>Location</th><th>Shift</th><th>Employee</th><th>Role</th></tr></thead><tbody>${{rows}}</tbody></table>`;
        const violations = data.violations.map(v => `<li><b>${{esc(v.type)}}</b> on ${{esc(v.date)}}: ${{esc(v.detail)}}</li>`).join('');
        document.getElementById('violations').innerHTML = violations || '<li>No violations</li>';
      }}

      function downloadJson() {{
        if (!latest) return alert('Generate schedule first.');
        const blob = new Blob([JSON.stringify(latest, null, 2)], {{type: 'application/json'}});
        saveBlob(blob, 'fobe_schedule.json');
      }}

      function downloadCsv() {{
        if (!latest) return alert('Generate schedule first.');
        const header = 'date,location,start,end,employee_id,role\\n';
        const body = latest.assignments.map(a => [a.date,a.location,a.start,a.end,a.employee_id,a.role].map(v => `"${{String(v).replaceAll('"', '""')}}"`).join(',')).join('\\n');
        const blob = new Blob([header + body], {{type: 'text/csv'}});
        saveBlob(blob, 'fobe_schedule.csv');
      }}

      function saveBlob(blob, filename) {{
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
      }}
    </script>
  </body>
</html>
"""


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    return generate_schedule(req)
