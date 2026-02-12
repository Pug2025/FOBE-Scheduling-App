from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import Assignment, Availability, Employee, Run, Settings, TimeOff, Violation

BLOCKS = ("morning", "evening")
SEASON_DEMAND = {
    "school": {"morning": 2, "evening": 2},
    "summer": {"morning": 3, "evening": 3},
}


@dataclass
class Candidate:
    employee: Employee
    score: float


def _is_available(availability_by_emp, emp_id, day_of_week, block):
    return availability_by_emp.get((emp_id, day_of_week, block), True)


def generate_run(db: Session, lock_previous: bool = False) -> Run:
    settings = db.get(Settings, 1)
    if not settings:
        raise ValueError("Settings must be configured before generation")

    latest = db.query(Run).order_by(Run.id.desc()).first()
    run = Run(seed=0)
    db.add(run)
    db.flush()

    employees = db.query(Employee).filter(Employee.active.is_(True)).all()
    availability = db.query(Availability).all()
    time_off = db.query(TimeOff).all()

    availability_by_emp = {(a.employee_id, a.day_of_week, a.block): a.available for a in availability}
    time_off_set = {(t.employee_id, t.date) for t in time_off}

    locks = {}
    if lock_previous and latest:
        for a in latest.assignments:
            if a.locked:
                locks[(a.date, a.block)] = a.employee_id

    worked = defaultdict(int)
    recent_manager_days = defaultdict(list)

    for offset in range(settings.horizon_days):
        work_date = settings.start_date + timedelta(days=offset)
        day = work_date.weekday()
        demand = SEASON_DEMAND.get(settings.season, SEASON_DEMAND["school"])

        for block in BLOCKS:
            if demand[block] <= 0:
                continue

            if (work_date, block) in locks:
                db.add(Assignment(run_id=run.id, date=work_date, block=block, employee_id=locks[(work_date, block)], locked=True))
                continue

            candidates = []
            for emp in employees:
                if (emp.id, work_date) in time_off_set:
                    continue
                if not _is_available(availability_by_emp, emp.id, day, block):
                    continue

                leadership_boost = 5 if emp.role in ("manager", "lead") and block == "morning" else 0
                fairness_penalty = worked[emp.id]

                manager_penalty = 0
                if emp.role == "manager":
                    prior_days = recent_manager_days[emp.id]
                    if len(prior_days) >= settings.manager_consecutive_days_off:
                        if all((work_date - d).days == i + 1 for i, d in enumerate(sorted(prior_days, reverse=True))):
                            manager_penalty = 100

                score = emp.leadership_score + leadership_boost - fairness_penalty - manager_penalty
                candidates.append(Candidate(employee=emp, score=score))

            candidates.sort(key=lambda c: (-c.score, c.employee.id))
            if not candidates:
                db.add(Violation(run_id=run.id, date=work_date, severity="hard", message=f"No available employee for {block}"))
                continue

            selected = candidates[0].employee
            db.add(Assignment(run_id=run.id, date=work_date, block=block, employee_id=selected.id, locked=False))
            worked[selected.id] += 1

            if selected.role == "manager":
                recent_manager_days[selected.id].append(work_date)
                recent_manager_days[selected.id] = [d for d in recent_manager_days[selected.id] if (work_date - d).days <= settings.manager_consecutive_days_off]

            if candidates[0].score < 0:
                db.add(Violation(run_id=run.id, date=work_date, severity="soft", message=f"Soft fairness violation on {block}"))

    db.commit()
    db.refresh(run)
    return run
