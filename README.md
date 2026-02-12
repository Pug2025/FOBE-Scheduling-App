# FOBE Scheduling App

## Project goal

Build a practical scheduling tool for FOBE operations that generates a workable two-week rota for:
- Greystones store
- Beach Shop
- Boat operations

The scheduler must balance coverage and leadership requirements while respecting employee constraints (availability, role qualification, max hours), and report violations when constraints cannot all be met.

## Current status

### ✅ Prototype generator implemented

This repository now includes a minimal **FastAPI prototype** in `/app` with:
- `GET /health` health check
- `GET /` web UI for pasting/editing scheduling input JSON
- `POST /generate` deterministic, greedy rules-aware schedule generation
- In-browser output rendering (assignments table + violations list)
- JSON and CSV export buttons

The prototype is intentionally lightweight:
- no database
- no authentication
- deployable on Render free tier

See [`app/README.md`](app/README.md) for run/deploy/API details.

## Next milestones

1. **Upgrade solver engine**
   - Replace/augment greedy assignment with OR-Tools CP-SAT optimization.
   - Add stronger objective balancing for fairness and minimum-hour targets.

2. **Improve rule coverage**
   - Add configurable shift templates and optional rest/clopen constraints.
   - Add richer soft-constraint weighting and violation severity.

3. **Manager workflow features**
   - Lock critical assignments and regenerate remaining schedule gaps.
   - Add editable scenario presets for seasonal operations.

4. **Data + persistence**
   - Introduce database-backed employee/rule storage.
   - Add versioned schedule drafts and change history.

5. **Production readiness**
   - Add tests (API + scheduling rule checks), structured logging, and CI.
   - Add export enhancements (payroll CSV variants, printable outputs).
