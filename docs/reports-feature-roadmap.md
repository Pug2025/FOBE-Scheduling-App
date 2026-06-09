# Reports Feature — Zero-Disruption Roadmap

**Status:** Planning (no code written yet)
**Goal:** Add a "Reports" section that serves the Greystones Financial Explorer HTML, with a new
"Report Only" user role and a per-user "Report Access" toggle in the admin panel — **without
disrupting any existing functionality on the live, in-season app.**

---

## Confirmed environment

- App is deployed on **Render**: <https://fobe-scheduling-app.onrender.com/>
- Live database is **PostgreSQL** (`DATABASE_URL`); local dev falls back to SQLite (`app/db.py`).
- Migrations run automatically on deploy via the Render start command:
  `alembic upgrade head && uvicorn app.main:app ...`
- Code on GitHub: `Pug2025/FOBE-Scheduling-App`. Pushing to `main` triggers a Render deploy.
- 87 passing tests in `tests/`; CI runs them on every PR (`.github/workflows/tests.yml`).
- Proven migration pattern for CHECK-constraint changes already exists:
  `alembic/versions/0005_expand_user_roles.py` (handles Postgres + SQLite).

---

## Feature requirements (from Jamie)

1. A new **"Reports"** section in the app showing the Greystones Financial Explorer, kept current.
2. It should **update automatically** when the weekly report is regenerated (fully automatic — the
   greystones-analysis skill pushes the new HTML to the app).
3. A new **"Report Only"** access level: those users can log in and see **only** the report.
4. In the admin panel, a **"Report Access"** checkbox next to each person.
5. Default access rules:
   - **Admin** → always has report access (default on), and can grant it to anyone via the checkbox.
   - **Report Only** → automatically and *only* has the report.
   - **Manager / View Only** → no access unless the checkbox is ticked.
   - **Everyone else, by default** → no access.

---

## Update mechanism (given Render hosting)

- Render's filesystem is **ephemeral** (wiped on restart/deploy), so the report **must be stored in
  the PostgreSQL database**, not as a file on disk.
- "Fully automatic" = the greystones-analysis skill, as its final step, **POSTs the generated HTML
  to the live app** (`POST /api/reports/upload`) authenticated with a secret token
  (`REPORT_UPLOAD_TOKEN`, a new Render env var, same pattern as the existing `BOOTSTRAP_TOKEN`).
- A manual **"Upload latest report"** button in the admin panel is the always-available fallback.

Weekly loop:
1. Run the weekly Greystones analysis → skill builds `Greystones Financial Explorer.html`.
2. Skill pushes the file to `POST /api/reports/upload` with the token.
3. App stores it as the new current report (keeps prior versions as history).
4. Anyone with report access sees the new week immediately — no deploy, no clicks.

---

## Core safety principle: add, never alter

Every change is a *new* thing alongside the old. Nothing existing is renamed, removed, or rewired.

- **New** column `report_access` (defaults `false`) → existing users behave exactly as today.
- **New** role `report_only` *added* to the allowed list → existing roles untouched.
- **New** table `report_documents` → touches nothing existing.
- **New** routes (`/reports`, `/api/reports/...`) → existing routes unmodified.
- **New** UI elements (admin checkbox, nav link) written to fail safe — if they error, the
  surrounding page keeps working.

The feature is **invisible and inert** until (a) a report is uploaded and (b) a box is ticked.

---

## The single biggest risk and how it's neutralised

The start command is `alembic upgrade head && uvicorn ...` — **if the migration fails, the app does
not boot.** A bad migration is the only thing that could take the site down. So:

1. **It's only a widening.** The role-constraint change *adds* `report_only`; no existing row can
   violate a wider rule, so it cannot fail on real data.
2. **It reuses the proven pattern** from `0005_expand_user_roles.py` (dialect-aware, Postgres +
   SQLite). Not a new technique.
3. **It's fully reversible** — a working `downgrade()` removes the column/table and restores the
   original constraint.
4. **It's rehearsed against a copy of production first** (Phase 2), never first-run on live.

---

## Phased plan

### Phase 0 — Snapshot & safety net (before touching anything)
- Back up the live database (Render backup and/or `pg_dump`) → known-good restore point.
- Confirm current deploy is healthy; note the current `main` commit SHA (instant rollback target).
- Confirm `pytest` → all 87 tests pass on `main` before adding anything.

### Phase 1 — Build on an isolated branch (live site untouched)
- All work on a feature branch (e.g. `feature/reports-access`); **never commit directly to `main`.**
- Build Stage 1:
  - Migration `0010`: add `report_access` column, widen role constraint to include `report_only`,
    add `report_documents` table (HTML + uploaded_at + uploaded_by + size).
  - `POST /api/reports/upload` (token- or admin-authenticated) → saves HTML to DB.
  - `GET /reports` page (login + report-access required), report rendered behind the login wall.
  - Login routing: Report-Only users land on `/reports` and are blocked elsewhere; others with
    access get a "Reports" nav link.
  - Admin panel: "Report Access" checkbox + "Report Only" role option + manual upload button.
- Guardrails: new column/role default to no access; upload endpoint returns a clean error (never a
  crash) if `REPORT_UPLOAD_TOKEN` is unset; new frontend JS isolated so failures don't break
  existing panels.

### Phase 2 — Rehearse the migration against real-shaped data
- Restore the Phase 0 backup into a throwaway local/staging PostgreSQL.
- `alembic upgrade head` → confirm it applies cleanly on real data.
- `alembic downgrade -1` then `upgrade head` → confirm reversible and repeatable.
- Only a migration that survives this rehearsal goes near production.

### Phase 3 — Prove nothing else broke (regression gate)
- Run the full existing 87-test suite on the branch → all must pass.
- Add new tests for the report feature (access rules, the four user-type behaviours, upload auth).
- CI runs the suite on the PR; `main` is protected by that check.
- Manual smoke test locally: log in as admin, manager, view-only, and a test report-only user;
  confirm every existing screen behaves exactly as before, plus the new Reports page works.

### Phase 4 — Controlled deploy with a tested escape hatch
- Open a PR into `main`, review the diff, confirm CI green.
- Deploy in a low-traffic window (not during busy operating hours).
- Merge → Render auto-deploys. Watch the deploy log confirm `alembic upgrade head` succeeds and the
  server boots.
- Run `scripts/post_deploy_smoke.sh` against the live URL (health + login).
- If anything looks wrong: Render one-click rollback to previous deploy; Phase 0 DB backup restores
  data if ever needed (won't be — migration is additive).

### Phase 5 — Turn it on gradually
- Feature affects no one until acted upon.
- Upload a test report via the manual button → verify the Reports page renders.
- Grant access to one test/manager account first → confirm they see Reports and others still don't.
- Then create real Report-Only users and tick boxes for staff.

### Phase 6 — Add the automation (separate, later, also reversible)
- Once Stage 1 is proven live, wire the greystones-analysis skill to auto-push, set
  `REPORT_UPLOAD_TOKEN` in Render.
- Purely additive to the skill and env; changes nothing about the running app for users; the manual
  upload button remains as fallback.

---

## Guarantees

| Concern | How it's protected |
|---|---|
| Migration takes the site down on boot | Widening-only + proven pattern + rehearsed on prod-data copy + reversible |
| A current user's access changes unexpectedly | New permissions default to OFF; nobody affected until a box is ticked |
| Existing scheduling/auth/timeclock breaks | All 87 existing tests must pass; CI enforces on the PR |
| A frontend bug breaks the admin/scheduler page | New UI is isolated and fails safe |
| Deploy goes wrong | Low-traffic window + one-click Render rollback + pre-migration DB backup |
| Automation misfires | Endpoint is inert without its token; manual upload is the fallback |

---

## Open items before build starts

1. Staging DB for the Phase 2 rehearsal, or a `pg_dump` of production to rehearse against locally.
2. Confirm feature-branch + PR workflow (no direct pushes to `main`) is approved.
3. (Phase 6) Access to set `REPORT_UPLOAD_TOKEN` in the Render dashboard.

---

## Unrelated security note

The GitHub access token is currently stored in plaintext inside the repo's git remote URL
(visible via `git remote -v`). Recommend rotating it (revoke on GitHub, use a credential helper).
Not urgent for this feature, but worth doing.
