# FOBE Scheduler Prototype (No DB / No Auth)

This app is a minimal FastAPI prototype designed to run locally and on Render free tier immediately.

## What it does
- `GET /health` returns `{"ok": true}` for health checks.
- `GET /` serves a simple HTML UI with:
  - prefilled sample JSON payload
  - **Generate 2-week schedule** button (`POST /generate`)
  - schedule table + violations display
  - JSON/CSV export links for the last generated schedule
- `POST /generate` accepts scheduling input and returns deterministic output.
- `GET /export/json` returns the last generated result.
- `GET /export/csv` returns assignment rows from the last generated result.

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open: `http://127.0.0.1:8000`

## Render deploy (free tier)
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health check path**: `/health`

## Quick smoke test
```bash
curl -s http://127.0.0.1:8000/health
```

```bash
curl -s -X POST http://127.0.0.1:8000/generate \
  -H 'Content-Type: application/json' \
  --data @- <<'JSON'
{
  "period": {"start_date": "2025-07-07", "weeks": 2},
  "season_rules": {
    "victoria_day": "2025-05-19",
    "june_30": "2025-06-30",
    "labour_day": "2025-09-01",
    "oct_31": "2025-10-31"
  },
  "hours": {
    "greystones": {"start": "08:30", "end": "17:30"},
    "beach_shop": {"start": "11:00", "end": "15:00"}
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
  "employees": [
    {
      "id": "jordan",
      "name": "Jordan",
      "role": "Store Clerk",
      "min_hours_per_week": 16,
      "max_hours_per_week": 40,
      "priority_tier": "B",
      "student": false,
      "availability": {"mon": ["08:30-17:30"], "tue": ["08:30-17:30"], "wed": ["08:30-17:30"], "thu": ["08:30-17:30"], "fri": ["08:30-17:30"], "sat": ["08:30-17:30"], "sun": ["08:30-17:30"]}
    }
  ],
  "unavailability": [],
  "history": {"manager_weekends_worked_this_month": 0},
  "shoulder_season": false
}
JSON
```

## Payload notes
- `period.start_date` and season-rule dates must be ISO format (`YYYY-MM-DD`).
- `employees[].availability` uses day keys `mon..sun` with one or more `HH:MM-HH:MM` windows.
- `employees[].student` blocks auto-scheduling on shoulder-season weekdays (manual edits can still add them).
- Unavailability dates are hard constraints.
- Max hours per week are hard constraints.
- Min hours per week are soft checks (reported in violations).
- When `shoulder_season=true`, min-hour makeup and min-hour violations are skipped.
