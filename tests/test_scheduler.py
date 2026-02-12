from datetime import date

from app.db import Base, SessionLocal, engine
from app.models import Assignment, Availability, Employee, Run, Settings
from app.scheduler import generate_run


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def seed(db):
    db.add(Settings(id=1, season="school", start_date=date(2026, 1, 5), horizon_days=5, manager_consecutive_days_off=2))
    db.add_all([
        Employee(name="Alex", role="manager", leadership_score=10, active=True),
        Employee(name="Blair", role="lead", leadership_score=8, active=True),
        Employee(name="Casey", role="staff", leadership_score=5, active=True),
    ])
    db.commit()
    employees = db.query(Employee).all()
    for e in employees:
        for d in range(7):
            db.add(Availability(employee_id=e.id, day_of_week=d, block="morning", available=True))
            db.add(Availability(employee_id=e.id, day_of_week=d, block="evening", available=True))
    db.commit()


def test_deterministic_generation():
    db = SessionLocal()
    seed(db)
    first = generate_run(db)
    baseline = [(a.date, a.block, a.employee_id) for a in db.query(Assignment).filter_by(run_id=first.id).order_by(Assignment.date, Assignment.block)]

    second = generate_run(db)
    repeat = [(a.date, a.block, a.employee_id) for a in db.query(Assignment).filter_by(run_id=second.id).order_by(Assignment.date, Assignment.block)]
    assert baseline == repeat


def test_lock_and_regenerate_preserves_locks():
    db = SessionLocal()
    seed(db)
    run1 = generate_run(db)
    first_assignment = db.query(Assignment).filter_by(run_id=run1.id).first()
    first_assignment.locked = True
    db.commit()

    run2 = generate_run(db, lock_previous=True)
    locked = db.query(Assignment).filter_by(run_id=run2.id, date=first_assignment.date, block=first_assignment.block).first()
    assert locked.employee_id == first_assignment.employee_id
    assert locked.locked is True
