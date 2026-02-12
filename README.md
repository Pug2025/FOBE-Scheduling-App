# FOBE-Scheduling-App

## FOBE Scheduling App — V1 Spec

## Goal

Generate a 2-week schedule that satisfies coverage needs for:

* Greystones (main store)
* Beach Shop (open only certain days)

While respecting:

* employee availability + vacations
* min/max hours (per week)
* role coverage (Store Clerk, Team Leader, Store Manager, Boat Captain)
* priority tiers (must-schedule vs filler)
* basic fatigue rules (rest time / no clopens)

The system should produce a schedule even when constraints conflict, and clearly list any violations.

---

## Roles + Core Constraints

### Roles

* Store Clerk
* Team Leader
* Store Manager
* Boat Captain

### Role rules (recommended defaults)

Greystones:

* At least 1 Team Leader OR Store Manager on duty whenever open.
* Close shift must include a Team Leader OR Store Manager.
* Open shift should include a Team Leader OR Store Manager.

Beach Shop:

* Requires 2 people during open hours.
* At least 1 Team Leader OR Store Manager must be assigned if Beach Shop is open (optional toggle; depends on how you run it).

Boat:

* Boat Captain scheduled only for boat shifts (separate “location” or “service”).
* If the boat operates on a given day, assign exactly 1 Boat Captain per boat run block (configurable).

---

## Locations + Hours

### Locations

1. Greystones
2. Beach Shop
3. Boat (optional “service” location; can be enabled/disabled weekly)

### Operating hours (configurable)

* Greystones: open days + open/close times
* Beach Shop: open days + open/close times
* Boat: operating days + run blocks (e.g., 10:00–12:00, 13:00–15:00)

---

## Coverage Requirements

Coverage requirements should be defined per location per day using either:
A) Shift templates (recommended)

* Open shift
* Mid shift
* Close shift

or
B) Time blocks (more flexible, more complexity)

* e.g., 10–12, 12–2, 2–4, 4–6

### Coverage rules (examples)

Greystones:

* Weekdays: 2 clerks minimum at all times
* Weekends: 3 clerks minimum at all times
* Always: 1 leader/manager on duty

Beach Shop:

* When open: 2 staff assigned

Boat:

* When operating: 1 captain per run block

All of these must be editable week-to-week.

---

## Employee Rules

Each employee profile contains:

* Name
* Role(s) qualified for (one person can have multiple)
* Weekly min hours (per week)
* Weekly max hours (per week)
* Availability by day (and ideally time windows)
* Vacation/unavailable dates
* Priority tier:

  * Tier A (must-get-hours / prioritize)
  * Tier B (normal)
  * Tier C (filler / gap coverage)

Recommended additional fields (high value, low effort):

* Max days per week
* “Can open” / “Can close”
* Student flag (hard availability)
* Preferred location(s)

---

## Scheduling Rules: Hard vs Soft

Hard constraints (never break):

* Unavailable/vacation
* Operating hours
* Role qualifications (captain can’t work store unless also qualified)
* Max hours (if truly hard cap)

Soft constraints (try best; show violations):

* Weekly minimum hours
* Fairness (weekend/closing rotation)
* Preferences (avoid clopens, consistent start times)
* “Use Tier C only when needed”

This separation is critical or the solver will either fail constantly or produce bad schedules.

---

## Fatigue / Practical Rules (defaults)

* Minimum rest between shifts: 11 hours (configurable)
* No close-to-open (“clopen”) unless allowed as emergency
* Max consecutive work days: 6 (configurable)

---

## Output Requirements

For each 2-week schedule:

* Schedule view by day + location
* Individual employee view (their shifts + totals)
* Totals per employee:

  * hours per week (Week 1 and Week 2)
  * days worked per week
  * opens/closes count
* Violations report:

  * uncovered blocks
  * missing leader/manager coverage
  * min hours not met
  * rest violations
* Export:

  * Printable PDF (later)
  * CSV for payroll (V1 or V1.1)
  * “Postable” grid image (later)

---

## Manager Workflow (important)

1. Enter/update:

   * employee availability + vacations
   * operating hours (Greystones/Beach/Boat)
   * coverage requirements
2. Generate schedule draft
3. Lock key assignments (captains, managers, known constraints)
4. Regenerate remaining gaps
5. Finalize + export

---

## Algorithm Approach (recommended)

Constraint solver (CP-SAT) using Google OR-Tools.

Why:

* This is exactly what CP-SAT is good at.
* You need hard/soft constraints, fairness, and “best possible” solutions.

---

## V1 Screens

1. Employees

   * list + edit
   * availability calendar
2. Locations & Hours
3. Coverage Rules

   * per location/day, define shift templates + headcount + leader requirement
4. Generate

   * run solver
   * show schedule + warnings
   * lock shifts + regenerate
5. Export

---

## V1 Questions That Must Be Answered (to finalize rules)

* Do you use fixed shift templates (Open/Mid/Close) or arbitrary start/end times?
* Does Beach Shop require two dedicated people, or can staff float between Greystones and Beach Shop during the same hour?
* Does Greystones require a Team Leader/Manager present at all times, or only for open/close?
* Is Store Manager coverage required daily, or can Team Leaders satisfy that rule?
* Is max hours a hard cap for everyone, or only for some staff?

---

## Next Build Steps

1. Confirm shift structure and whether Beach Shop staff can float.
2. Confirm leader/manager coverage rules for Greystones + Beach Shop.
3. Build data model + basic CRUD UI for employees/availability.
4. Implement CP-SAT schedule generation for one week, then extend to 2 weeks.
5. Add lock/regenerate + violations report.

---

## Prototype Implementation (FastAPI)

A lightweight FastAPI prototype can be used to validate scheduling workflows before full production UI work.

### Suggested scope for the prototype

* Build REST endpoints for employees, availability, locations, and coverage rules.
* Add a `/generate` endpoint that runs scheduling logic and returns a draft schedule plus violations.
* Keep all hard constraints enforced, and return soft-constraint violations in the response payload.
* Start with in-memory or JSON-backed persistence to speed up iteration.

### Suggested API surface

* `GET /health` — health check.
* `GET/POST /employees` — list and create employees.
* `GET/POST /locations` — list and create locations/hours.
* `GET/POST /coverage-rules` — manage required staffing by day/template.
* `POST /generate` — generate a 2-week draft schedule.
* `POST /lock-and-regenerate` — lock selected assignments and regenerate remaining gaps.

### Prototype deliverables

* OpenAPI docs from FastAPI for quick stakeholder review.
* JSON export endpoint for payroll preprocessing.
* Violation report endpoint mirroring the Output Requirements section above.
