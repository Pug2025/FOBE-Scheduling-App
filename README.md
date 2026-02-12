# FOBE Scheduler V1

Production-oriented single-service scheduling application built with FastAPI, SQLAlchemy, Alembic, SQLite, Jinja, HTMX, and Tailwind.

## Features
- Deterministic scheduling generation
- Lock-and-regenerate workflow
- Persistent runs, assignments, and violations
- Manager-facing dashboard and CRUD pages
- Availability grid editor
- Time-off management
- CSV/JSON exports
- Printable schedule view

## Tech Stack
- FastAPI (web + server routing)
- SQLAlchemy ORM (data model)
- Alembic migrations (schema lifecycle)
- SQLite file database (`fobe_scheduler.db`)
- Jinja templates + HTMX + Tailwind UI

## Project Layout
```
app/
  db.py
  main.py
  models.py
  scheduler.py
alembic/
  env.py
  versions/0001_initial.py
templates/pages/
tests/
```

## Local Setup
1. Create environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Apply migrations:
   ```bash
   alembic upgrade head
   ```
3. Start app:
   ```bash
   uvicorn app.main:app --reload
   ```
4. Open `http://127.0.0.1:8000`.

## Render Deployment (Free Tier)
Create one Web Service:
- Build command: `pip install -r requirements.txt && alembic upgrade head`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Persistent disk recommended to retain SQLite DB.

## Manager Workflow
1. **Settings:** configure season, horizon, schedule start date, and manager consecutive-day-off rule.
2. **Employees:** add/update active roster with role and leadership score.
3. **Availability:** mark weekly morning/evening availability per employee.
4. **Time Off:** enter dated exceptions.
5. **Generate & Review:** run schedule, inspect violations, and lock critical assignments.
6. **Lock & Regenerate:** re-run generation while preserving locked placements.
7. **Exports:** download latest schedule as JSON or CSV, or print.

## Scheduling Behavior Summary
- Season-driven demand blocks (`school` vs `summer`)
- Leadership preference for key blocks
- Hard constraints: availability and time off
- Soft constraint: fairness workload balancing
- Manager consecutive-day-off rule penalizes violating assignments
- Deterministic selection: sorted scoring and stable tie-breaking

## Tests
Run:
```bash
pytest
```
Includes:
- Deterministic generation test
- Lock-and-regenerate test
- API smoke test

## Current Prototype Status (Render-friendly)

The app now includes a minimal no-database/no-auth prototype scheduler under `app/main.py` focused on reliable startup and deployment.

### Run locally
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Endpoints
- `GET /health` → `{"ok": true}`
- `GET /` → built-in JSON input UI + schedule table + violations + export links
- `POST /generate` → deterministic 2-week schedule JSON
- `GET /export/json` and `GET /export/csv` → last generated schedule exports

### Render free tier
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Recommended health check path: `/health`

### Near-term milestones
1. Improve optimization quality using OR-Tools while keeping deterministic tie-breaking.
2. Add richer reporting for fairness and leadership coverage.
3. Reintroduce persistence as an optional feature (not required for baseline deployability).
