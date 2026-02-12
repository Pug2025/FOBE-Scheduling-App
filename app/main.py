from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, timedelta
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


Role = Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]


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
    week1_days: int = 0
    week2_days: int = 0
    weekend_days: int = 0
    locations: dict[str, int] = Field(default_factory=lambda: {"Greystones": 0, "Beach Shop": 0, "Boat": 0})


class ViolationOut(BaseModel):
    date: str
    type: Literal["coverage_gap", "leader_gap", "manager_consecutive_days_off", "role_missing", "beach_shop_gap"]
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


def _week_index(day: date, start: date) -> int:
    return ((day - start).days // 7) + 1


def _choose_pair_for_manager_off(days: list[date], season: SeasonRules, extras: dict[date, int]) -> tuple[date, date]:
    pairs = []
    for i in range(len(days) - 1):
        d1, d2 = days[i], days[i + 1]
        score = int(_is_greystones_open(d1, season)) + int(_is_greystones_open(d2, season))
        score += extras.get(d1, 0) + extras.get(d2, 0)
        pairs.append((score, d1, d2))
    pairs.sort(key=lambda x: (x[0], x[1]))
    return (pairs[0][1], pairs[0][2]) if pairs else (days[0], days[0])


def _generate(payload: GenerateRequest) -> GenerateResponse:
    emp_map = {e.id: e for e in sorted(payload.employees, key=lambda x: x.id)}
    unavail = {(u.employee_id, u.date) for u in payload.unavailability}
    extras = {x.date: x.extra_people for x in payload.extra_coverage_days}
    start_date = payload.period.start_date
    all_days = _daterange(start_date, payload.period.weeks * 7)

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_assigned: dict[date, set[str]] = defaultdict(set)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)

    manager_ids = [e.id for e in emp_map.values() if e.role == "Store Manager"]
    forced_manager_off: set[date] = set()
    if payload.leadership_rules.manager_two_consecutive_days_off_per_week and manager_ids:
        for ws in [d for d in all_days if d.weekday() == 0]:
            week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
            if week_days:
                a, b = _choose_pair_for_manager_off(week_days, payload.season_rules, extras)
                forced_manager_off.update({a, b})

    def eligible(day: date, role: Role, start: str, end: str, ignore_max: bool = False) -> list[Employee]:
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        out: list[Employee] = []
        for e in emp_map.values():
            if e.role != role:
                continue
            if (e.id, day) in unavail:
                continue
            if role == "Store Manager" and day in forced_manager_off:
                continue
            wk = _week_index(day, start_date)
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
            e.name,
        ))
        return out

    def assign_one(day: date, location: str, start: str, end: str, role: Role, needed: int, ignore_max: bool = False):
        for e in eligible(day, role, start, end, ignore_max=ignore_max)[:needed]:
            assignments.append({
                "date": day,
                "location": location,
                "start": start,
                "end": end,
                "employee_id": e.id,
                "employee_name": e.name,
                "role": role,
            })
            weekly_hours[(e.id, _week_index(day, start_date))] += _hours_between(start, end)
            daily_assigned[day].add(e.id)

    for d in all_days:
        if _is_greystones_open(d, payload.season_rules):
            g_start, g_end = payload.hours.greystones.start, payload.hours.greystones.end
            needed = (payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff) + extras.get(d, 0)
            assign_one(d, "Greystones", g_start, g_end, "Store Manager", 1)
            manager_on = any(a for a in assignments if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Store Manager")
            lead_need = max(payload.leadership_rules.min_team_leaders_every_open_day, 2 if (_is_weekend(d) and not manager_on) else 1)
            assign_one(d, "Greystones", g_start, g_end, "Team Leader", lead_need)
            currently = len([a for a in assignments if a["date"] == d and a["location"] == "Greystones"])
            assign_one(d, "Greystones", g_start, g_end, "Store Clerk", max(0, needed - currently))
            if len([a for a in assignments if a["date"] == d and a["location"] == "Greystones"]) < needed:
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
                weekly_hours[(e.id, _week_index(d, start_date))] += _hours_between(g_start, g_end)
                daily_assigned[d].add(e.id)
            else:
                violations.append(ViolationOut(date=d.isoformat(), type="role_missing", detail="Missing Boat Captain"))

        if _is_beach_shop_open(d, payload.season_rules):
            b_start, b_end = payload.hours.beach_shop.start, payload.hours.beach_shop.end
            needed = payload.coverage.beach_shop_staff
            assigned_before = len([a for a in assignments if a["date"] == d])
            assign_one(d, "Beach Shop", b_start, b_end, "Team Leader", 1)
            assign_one(d, "Beach Shop", b_start, b_end, "Store Clerk", max(0, needed - (len([a for a in assignments if a["date"] == d]) - assigned_before)))

    # Validate manager consecutive off rule.
    for ws in [d for d in all_days if d.weekday() == 0]:
        week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
        for manager_id in manager_ids:
            work = [manager_id in daily_assigned[d] for d in week_days]
            has_pair = any((not work[i]) and (not work[i + 1]) for i in range(len(work) - 1))
            if payload.leadership_rules.manager_two_consecutive_days_off_per_week and not has_pair:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_consecutive_days_off", detail=f"Manager {emp_map[manager_id].name} lacks consecutive days off"))

    totals: dict[str, TotalsOut] = {e.id: TotalsOut() for e in emp_map.values()}
    for e in emp_map.values():
        for d in all_days:
            wk = _week_index(d, start_date)
            if e.id in daily_assigned[d]:
                if wk == 1:
                    totals[e.id].week1_days += 1
                elif wk == 2:
                    totals[e.id].week2_days += 1
                if _is_weekend(d):
                    totals[e.id].weekend_days += 1
        totals[e.id].week1_hours = round(weekly_hours[(e.id, 1)], 2)
        totals[e.id].week2_hours = round(weekly_hours[(e.id, 2)], 2)

    for a in assignments:
        totals[a["employee_id"]].locations[a["location"]] += 1

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
    return {
        "period": {"start_date": "2025-07-07", "weeks": 2},
        "season_rules": {"victoria_day": "2025-05-19", "june_30": "2025-06-30", "labour_day": "2025-09-01", "oct_31": "2025-10-31"},
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
    }


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    payload = json.dumps(_sample_payload_dict())
    return f"""
<!doctype html><html><head><meta charset='utf-8'><title>FOBE Scheduler</title></head>
<body style='font-family:Inter,system-ui,sans-serif;margin:0;background:#f1f5f9;color:#0f172a;'>
<div style='max-width:1200px;margin:0 auto;padding:1.2rem;'>
<h1>FOBE Schedule Builder</h1>
<p id='feedback' style='display:none;'></p>
<div style='display:flex;gap:.5rem;margin-bottom:1rem;'><button onclick='loadSampleData()'>Load Example Data</button><button onclick='runGenerate()'>Generate Schedule</button></div>
<section style='background:#fff;padding:1rem;margin-bottom:1rem;'>
<h3>Employees</h3>
<p>Role caps: 1 Store Manager, 2 Team Leaders. Names are used throughout output (ID column removed).</p>
<p><strong>Priority glossary:</strong> A = highest scheduling priority, B = regular priority, C = flexible/fill-in priority.</p>
<button onclick='addEmployeeRow()'>Add Employee</button>
<table style='width:100%;margin-top:.5rem;'><thead><tr><th>Name</th><th>Role</th><th>Min hrs/wk</th><th>Max hrs/wk</th><th>Priority</th><th></th></tr></thead><tbody id='employee_rows'></tbody></table>
</section>
<section style='background:#fff;padding:1rem;margin-bottom:1rem;'>
<h3>Extra coverage days</h3>
<button onclick='addExtraRow()'>Add Extra Day</button>
<table style='width:100%;margin-top:.5rem;'><thead><tr><th>Date</th><th>Extra people</th><th></th></tr></thead><tbody id='extra_rows'></tbody></table>
</section>
<section style='background:#fff;padding:1rem;'>
<h3>Calendar Output</h3>
<div id='result'>Generate to view calendar.</div>
</section>
</div>
<script>
const ROLE_OPTIONS = ['Store Clerk','Team Leader','Store Manager','Boat Captain'];
const DAY_KEYS = ['mon','tue','wed','thu','fri','sat','sun'];
const SAMPLE_PAYLOAD = {payload};

function showMessage(msg) {{ const el=document.getElementById('feedback'); el.style.display='block'; el.textContent=msg; }}
function slugifyName(name, idx) {{ return name.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'') || `emp_${{idx+1}}`; }}

function roleCounts() {{
  const roles=[...document.querySelectorAll('.emp-role')].map(s=>s.value);
  return {{ manager: roles.filter(r=>r==='Store Manager').length, lead: roles.filter(r=>r==='Team Leader').length }};
}}
function refreshRoleOptions() {{
  const c=roleCounts();
  document.querySelectorAll('.emp-role').forEach(sel=>{{
    const current=sel.value;
    sel.innerHTML = ROLE_OPTIONS.map(r=>{{
      const disabled = (r==='Store Manager' && c.manager>=1 && current!=='Store Manager') || (r==='Team Leader' && c.lead>=2 && current!=='Team Leader');
      return `<option value="${{r}}" ${{current===r?'selected':''}} ${{disabled?'disabled':''}}>${{r}}</option>`;
    }}).join('');
  }});
}}

function employeeRowHtml(emp={{}}) {{
  return `<tr><td><input class='emp-name' value='${{emp.name||''}}'></td><td><select class='emp-role' onchange='refreshRoleOptions()'>${{ROLE_OPTIONS.map(r=>`<option value="${{r}}" ${{(emp.role||'Store Clerk')===r?'selected':''}}>${{r}}</option>`).join('')}}</select></td><td><input type='number' class='emp-min' value='${{emp.min_hours_per_week??16}}'></td><td><input type='number' class='emp-max' value='${{emp.max_hours_per_week??40}}'></td><td><select class='emp-priority'><option value='A' ${{emp.priority_tier==='A'?'selected':''}}>A</option><option value='B' ${{(!emp.priority_tier||emp.priority_tier==='B')?'selected':''}}>B</option><option value='C' ${{emp.priority_tier==='C'?'selected':''}}>C</option></select></td><td><button onclick='this.closest("tr").remove();refreshRoleOptions()'>Remove</button></td></tr>`;
}}
function addEmployeeRow(emp={{}}) {{ document.getElementById('employee_rows').insertAdjacentHTML('beforeend', employeeRowHtml(emp)); refreshRoleOptions(); }}
function extraRowHtml(row={{}}) {{ return `<tr><td><input type='date' class='extra-date' value='${{row.date||''}}'></td><td><input type='number' min='1' class='extra-people' value='${{row.extra_people||1}}'></td><td><button onclick='this.closest("tr").remove()'>Remove</button></td></tr>`; }}
function addExtraRow(row={{}}) {{ document.getElementById('extra_rows').insertAdjacentHTML('beforeend', extraRowHtml(row)); }}

function collectPayload() {{
  const employees=[...document.querySelectorAll('#employee_rows tr')].map((tr,idx)=>{{
    const name=tr.querySelector('.emp-name').value.trim();
    return {{id:slugifyName(name,idx),name,role:tr.querySelector('.emp-role').value,min_hours_per_week:Number(tr.querySelector('.emp-min').value||0),max_hours_per_week:Number(tr.querySelector('.emp-max').value||40),priority_tier:tr.querySelector('.emp-priority').value,availability:Object.fromEntries(DAY_KEYS.map(day=>[day,['08:30-17:30']]))}};
  }}).filter(e=>e.name);
  const extra_coverage_days=[...document.querySelectorAll('#extra_rows tr')].map(tr=>({{date:tr.querySelector('.extra-date').value,extra_people:Number(tr.querySelector('.extra-people').value||1)}})).filter(r=>r.date);
  return {{...SAMPLE_PAYLOAD, employees, extra_coverage_days}};
}}

function renderCalendar(assignments) {{
  const byDate = assignments.reduce((m,a)=>{{ (m[a.date] ||= []).push(a); return m; }},{{}});
  const dates = Object.keys(byDate).sort();
  return `<table style='width:100%;border-collapse:collapse;'><thead><tr><th>Date</th><th>Manager</th><th>Team Leaders</th><th>Clerks</th><th>Captain</th></tr></thead><tbody>${{dates.map(d=>{{
    const day=byDate[d];
    const names=(role,loc)=>day.filter(a=>a.role===role && (!loc || a.location===loc)).map(a=>a.employee_name).join(', ') || '—';
    return `<tr><td>${{d}}</td><td>${{names('Store Manager','Greystones')}}</td><td>${{names('Team Leader','Greystones')}}</td><td>${{names('Store Clerk','Greystones')}}</td><td>${{names('Boat Captain','Boat')}}</td></tr>`;
  }}).join('')}}</tbody></table>`;
}}

async function runGenerate() {{
  const res=await fetch('/generate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(collectPayload())}});
  const json=await res.json();
  if(!res.ok) {{ showMessage('Generate failed'); return; }}
  document.getElementById('result').innerHTML = renderCalendar(json.assignments) + (json.violations.length?`<h4>Rule checks</h4><ul>${{json.violations.map(v=>`<li>${{v.date}}: ${{v.detail}}</li>`).join('')}}</ul>`:'<p>No violations.</p>');
  showMessage(`Generated ${{json.assignments.length}} assignments.`);
}}

function loadSampleData() {{
  document.getElementById('employee_rows').innerHTML='';
  SAMPLE_PAYLOAD.employees.forEach(addEmployeeRow);
  document.getElementById('extra_rows').innerHTML='';
  (SAMPLE_PAYLOAD.extra_coverage_days||[]).forEach(addExtraRow);
}}
loadSampleData();
</script></body></html>
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
    writer.writerow(["date", "location", "start", "end", "employee_id", "employee_name", "role"])
    for a in _LAST_RESULT.assignments:
        writer.writerow([a.date, a.location, a.start, a.end, a.employee_id, a.employee_name, a.role])
    return Response(content=out.getvalue(), media_type="text/csv")
