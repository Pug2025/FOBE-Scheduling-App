# FOBE Scheduler Prototype (`/app`)

This prototype is a minimal FastAPI app that generates a deterministic, rules-aware 2-week schedule with no database and no authentication.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Render free-tier deploy (GitHub-connected)

1. Push this repo to GitHub.
2. In Render, create a **New Web Service** and connect the repository.
3. Configure:
   - **Runtime**: Python
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Deploy.
5. Verify `https://<your-service>.onrender.com/health` returns `{"ok": true}`.

## API endpoints

- `GET /health` → `{ "ok": true }`
- `GET /` → Web form with sample payload + generation and export buttons
- `POST /generate` → Returns assignments, totals by employee, and violations

## Curl example for `/generate`

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "period": {"start_date": "2026-07-06", "weeks": 2},
    "season_rules": {
      "victoria_day": "2026-05-18",
      "june_30": "2026-06-30",
      "labour_day": "2026-09-07",
      "oct_31": "2026-10-31"
    },
    "hours": {
      "greystones": {"start": "08:30", "end": "17:30"},
      "beach_shop": {"start": "12:00", "end": "16:00"}
    },
    "coverage": {
      "greystones_weekday_staff": 3,
      "greystones_weekend_staff": 4,
      "beach_shop_staff": 2
    },
    "leadership_rules": {
      "min_team_leaders_every_open_day": 1,
      "weekend_team_leaders_if_manager_off": 2,
      "manager_two_consecutive_days_off_per_week": true,
      "manager_min_weekends_per_month": 2
    },
    "employees": [],
    "unavailability": [],
    "history": {"manager_weekends_worked_this_month": 0}
  }'
```

## Input/output summary

### Input

`POST /generate` accepts:
- planning period (`start_date`, `weeks`)
- season date rules used to determine open days
- store/location hours
- headcount coverage targets
- leadership constraints
- employee profiles and availability windows
- one-off unavailability entries
- manager history metadata

### Output

`POST /generate` returns:
- `assignments`: day/location/shift/employee/role allocations
- `totals_by_employee`: weekly hours, day counts, weekend days, location counts
- `violations`: rule or coverage issues (e.g., `coverage_gap`, `leader_gap`, `role_missing`)
