# FOBE Scheduler Prototype (FastAPI)

This folder contains a minimal FastAPI prototype for generating a deterministic 2-week FOBE schedule draft (no database, no authentication).

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000/`.

## Render free-tier deployment (GitHub-connected)

1. Push this repo to GitHub.
2. In Render, create a **Web Service** from that GitHub repo.
3. Use:
   - **Environment**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Deploy. Health check endpoint is `GET /health`.

## Example `/generate` call

```bash
curl -X POST http://localhost:8000/generate \
  -H 'Content-Type: application/json' \
  -d @sample.json
```

You can copy the sample payload from the homepage textarea into `sample.json`.

## Input summary

`POST /generate` accepts:

- `period`: start date and number of weeks (prototype expects 2 weeks)
- `season_rules`: Victoria Day / June 30 / Labour Day / Oct 31 cutoffs
- `hours`: operating windows for Greystones and Beach Shop
- `coverage`: headcount requirements
- `leadership_rules`: leader + manager policy toggles
- `employees`: roles, min/max hours, tier, and daily availability windows
- `unavailability`: specific employee date exceptions
- `history`: manager weekends worked this month (kept for future solver upgrades)

## Output summary

`POST /generate` returns:

- `assignments`: dated shift allocations by location and role
- `totals_by_employee`: week 1 + week 2 totals, weekend days, and location counts
- `violations`: unsatisfied hard/soft rules (coverage, leadership, manager days off, etc.)

Also available after running `/generate`:

- `GET /export/json` for JSON download
- `GET /export/csv` for assignment CSV download
