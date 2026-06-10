# Security Hardening Roadmap — Zero-Disruption Plan

**Status:** Planning (no code written yet)
**Source:** Security audit of 2026-06-10 (focus: financial report handling), prepared ahead of
review by the FOBE board's cybersecurity professional.
**Prime directive:** the app is in active mid-season use. Every change ships in a way that
cannot disrupt scheduling, time clock, kiosk, day-off requests, or report viewing.

---

## Audit findings being addressed

| ID | Severity | Finding |
|----|----------|---------|
| H1 | High | No brute-force protection on `/auth/login` (also `/auth/bootstrap`, kiosk unlock, kiosk PIN entry) |
| H2 | High | No HSTS header (HTTPS downgrade window) |
| M1 | Medium | No site-wide security headers (clickjacking, referrer, sniffing) |
| M2 | Medium | No audit trail for who *viewed* the financial report or who granted/revoked access |
| M3 | Medium | Account enumeration: disabled accounts return a distinct 403 on login |
| M4 | Medium | Stage-2 upload token is a single shared static secret (process/hygiene item) |
| M5 | Medium | 14-day sessions, no idle timeout, no "revoke all sessions" control |
| L2–L6 | Low | Password policy depth, crypt() fallback, bootstrap token compare + env removal, app-level encryption note, dependency scanning |

---

## How we guarantee nothing breaks

### 1. The proven delivery pipeline (used for the Reports feature)
Every phase ships exactly the way the Reports feature shipped, which deployed with zero
downtime:

1. Work on an isolated feature branch — `main` stays frozen.
2. Full local test suite must pass (99 tests today; grows each phase).
3. New tests written for each behavior added or deliberately changed.
4. Fresh-eyes verification agent audits the diff before any deploy.
5. Manual verification on the local preview server (login as each role; exercise kiosk,
   scheduler, reports).
6. PR → GitHub CI runs the suite on a clean checkout → merge only on green.
7. Deploy in a quiet evening window; watch Render logs for clean boot.
8. Post-deploy smoke checks against the live URL (health, login, role redirects, reports,
   kiosk page) — `scripts/post_deploy_smoke.sh` plus manual route checks.
9. Render one-click rollback standing by; phases with no DB migration are trivially
   rollback-safe.

### 2. Small, independent PRs — never a big bang
Each phase is its own small PR that can be deployed, verified, and rolled back
independently. No phase depends on a later one. If any phase misbehaves, we roll back
that phase only.

### 3. Migration discipline
Only Phase 3 (audit trail) touches the database, and it is **additive only** (one new
table — no changes to existing tables at all). It reuses the migration pattern already
proven twice in production (0005, 0010), ships with a tested `downgrade()`, and is
rehearsed locally (upgrade → downgrade → upgrade) before deploy. All other phases are
**zero-migration**, so a failed deploy cannot poison the boot sequence
(`alembic upgrade head && uvicorn ...`).

### 4. Known breakage traps — identified in advance, designed around

These are the specific ways a naive implementation of the audit fixes WOULD break the
site. Each one is explicitly handled:

| Trap | Naive fix that breaks things | Our design |
|------|------------------------------|------------|
| **Report iframe vs clickjacking header** | `X-Frame-Options: DENY` site-wide blocks `/reports` from embedding `/api/reports/current/content` → the financial report goes blank | Use `SAMEORIGIN` / CSP `frame-ancestors 'self'`, and add an automated test asserting the report content endpoint remains embeddable same-origin |
| **Kiosk shared IP vs rate limiting** | Aggressive per-IP lockout: all Greystones staff sit behind ONE public IP. A few mistyped PINs/passwords would lock the whole store out of the time clock | Per-**account** lockout with escalating backoff as the primary control; per-IP limits set generously (high threshold, short window) as a secondary net; kiosk PIN throttle keyed per kiosk session, not per IP; admin unlock path documented |
| **Blanket CSP vs inline scripts** | A strict `Content-Security-Policy` breaks every page — the whole frontend uses inline `<script>` blocks, and `view_only.html` loads Google Fonts | Defer full CSP (it requires a frontend refactor). Ship the headers that are safe everywhere now (`X-Frame-Options`, `Referrer-Policy`, `X-Content-Type-Options`, `Permissions-Policy`); keep CSP as a later, separately-tested item |
| **HSTS vs local development** | Sending HSTS unconditionally pollutes local-dev behavior | Send HSTS only when the request is HTTPS (the app already has `request_is_https()` for exactly this kind of check); start with a moderate max-age, raise to 1 year after a clean week |
| **Generic login errors vs existing contract** | Changing disabled-login from 403→401 silently breaks `tests/test_auth_api.py:321` and changes the message users see (frontend maps 401 → "Incorrect email or password") | The contract change is *deliberate and documented*: tests updated intentionally, frontend message verified, and the tradeoff (disabled users see the generic message; admins can tell them why) recorded for the board reviewer |
| **Session changes vs mid-season staff** | Shortening session lifetime or adding idle timeout logs every staff member out unexpectedly — "the app broke" | Session-lifetime policy changes are a **decision gate** (see Phase 4): nothing changes for regular staff without explicit owner approval. The purely-additive "revoke all sessions" admin control ships first |
| **SESSION_SECRET rotation hazard** | "Rotate all secrets" sounds like good hygiene — but `SESSION_SECRET` doubles as the time-clock PIN pepper (`app/timeclock.py:46-52`). Rotating it would silently break EVERY staff clock-in PIN | Documented here as a standing warning. If rotation is ever needed: set `TIME_CLOCK_PIN_PEPPER` to the *old* value first, then rotate `SESSION_SECRET` — or re-issue all PINs deliberately |
| **In-memory rate limiter vs multi-instance future** | A memory-based limiter silently stops being global if the app ever scales to 2+ instances | Acceptable today (single Render instance); limitation documented in code comments and here |

---

## Phases

### Phase 0 — Baseline (before each phase, ~5 min)
- Confirm live site healthy; note current deploy SHA (rollback target).
- Confirm `pytest` green on `main`.
- Render automatic DB backups verified present (Phase 3 only).

### Phase 1 — PR #1 "Edge hardening" (H2, M1, M3, L4) — no migration
**Scope:**
- Response-header middleware (extends the existing middleware at `app/main.py:90`):
  - `Strict-Transport-Security` (HTTPS responses only) — H2
  - `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: no-referrer`,
    `X-Content-Type-Options: nosniff`, minimal `Permissions-Policy` — M1
- Unify all login failures (wrong password / unknown email / disabled account) to one
  generic 401; equalize the timing path (verify against a dummy hash when the email is
  unknown) — M3. Applies to `/auth/login` and the kiosk unlock endpoint.
- Constant-time compare for the bootstrap token (L4 code half).
**Tests:** header assertions on representative routes; report-iframe embeddability test;
updated login-failure tests; full suite.
**Ops (Jamie):** confirm `BOOTSTRAP_TOKEN` is removed from Render env (L4 ops half).
**Rollback:** one-click; no schema involvement.

### Phase 2 — PR #2 "Login protection" (H1) — no migration
**Scope:**
- In-memory rate limiting + lockout, designed around the kiosk trap:
  - Per-account: escalating delay/lockout after N consecutive failures (auto-expiring).
  - Per-IP: generous ceiling as a backstop (shared park IP must never trip on normal use).
  - Kiosk PIN attempts: throttled per kiosk session.
  - Bootstrap endpoint: tight limit (it should never be hit in production anyway).
- Clear 429 responses with Retry-After; frontend shows a friendly "too many attempts" message.
**Tests:** lockout triggers and expires; successful login resets the counter; normal login
flow unaffected; kiosk shared-IP scenario simulated; full suite.
**Rollback:** one-click; limiter state is memory-only and vanishes on restart.

### Phase 3 — PR #3 "Audit trail" (M2) — additive migration 0011
**Scope:**
- New `security_events` table (event type, user id/email snapshot, target, timestamp,
  source IP): report viewed, report uploaded, report access granted/revoked, role changed,
  login lockout tripped, session revocation.
- Write-path hooks in the relevant endpoints (report content fetch, admin user patch, upload).
- Small read-only "Recent report activity" list in the admin panel (admin-only endpoint).
**Migration safety:** additive-only new table; up/down/up rehearsed locally; no existing
table is touched, so even a worst-case failure cannot corrupt existing data.
**Tests:** events written for each hooked action; viewing requires admin; full suite.
**Rollback:** code rollback is safe immediately (old code ignores the new table).

### Phase 4 — PR #4 "Session controls" (M5)
**DECIDED (2026-06-10, Jamie):** no idle timeout — for admin or anyone. Phase 4 ships
only the purely-additive control:
- Admin "Sign out all sessions" button per user (deletes their session rows — the
  mechanism already exists for password resets).
Session lifetime stays at 14 days for all roles. If the board reviewer pushes back,
revisit then; the M5 finding is partially mitigated by the revoke-all control plus the
existing automatic session invalidation on disable/password-reset.

### Phase 5 — Ops & supply chain (M4, L6, L3, L2) — mostly process
- M4: `REPORT_UPLOAD_TOKEN` hygiene rules (env-only storage, rotation cadence) — lands
  together with Stage 2 (automatic weekly report push), not before.
- L6: enable Dependabot on the GitHub repo (additive; its PRs go through the same CI gate).
- L3: remove the `crypt()` fallback from `app/security.py` (bcrypt is guaranteed by
  requirements; removal prevents silent hash downgrade). Tiny PR, full suite.
- L2: optional admin-password minimum raise — owner's call, zero code risk.

---

## Per-phase verification protocol (every phase, no exceptions)
1. Full suite locally (all existing + new tests).
2. Fresh-eyes verification agent audit of the diff.
3. Preview-server manual pass: admin, manager, view-only, report-only logins; kiosk page;
   schedule generation; report view; admin panel.
4. PR + CI green on clean checkout.
5. Quiet-window deploy; Render log watch; live smoke checks (health, login, role
   redirects, `/reports`, kiosk).
6. Rollback rehearsed mentally before merge: "if X looks wrong, click rollback, site
   returns to prior state, nothing persisted breaks."

## Deliberately NOT doing (and why)
- **No blanket Content-Security-Policy yet** — guaranteed to break the inline-script
  frontend; needs its own refactor + test cycle.
- **No session shortening without explicit approval** — surprise logouts mid-season
  violate the prime directive.
- **No SESSION_SECRET rotation** — it peppers the time-clock PINs (see traps table).
- **No big combined PR** — each phase independently shippable and reversible.

## Decisions needed from Jamie before/while building
1. Greenlight Phase 1 + 2 first (recommended)? They close both High findings with zero
   schema risk.
2. ~~Phase 4 idle-timeout policy~~ **DECIDED 2026-06-10: no idle timeout for anyone.**
   Phase 4 = revoke-all-sessions button only.
3. Preferred deploy window (evenings after close?).
4. ~~Confirm in Render: `BOOTSTRAP_TOKEN` env var removed~~ **CHECKED 2026-06-10 via live
   probe: still set — removal steps given to Jamie (Render → fobe-scheduling-app →
   Environment → delete `BOOTSTRAP_TOKEN` → Save). Re-verify by probe after removal
   (expect 503 "not configured" instead of 403 "invalid token").**
