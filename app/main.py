import csv
import io
import json
from datetime import date, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import Assignment, Availability, Employee, Run, Settings, TimeOff, Violation
from app.scheduler import BLOCKS, generate_run

app = FastAPI(title="FOBE Scheduler")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        if not db.get(Settings, 1):
            db.add(Settings(id=1, season="school", start_date=date.today(), horizon_days=14, manager_consecutive_days_off=2))
            db.commit()
    finally:
        db.close()


def _sidebar_context(db: Session):
    latest = db.query(Run).order_by(Run.id.desc()).first()
    return {"latest_run": latest}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "pages/dashboard.html",
        {
            "request": request,
            "employees": db.query(Employee).count(),
            "time_off": db.query(TimeOff).count(),
            "violations": db.query(Violation).count(),
            **_sidebar_context(db),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("pages/settings.html", {"request": request, "settings": db.get(Settings, 1), **_sidebar_context(db)})


@app.post("/settings")
def update_settings(
    season: str = Form(...),
    start_date: str = Form(...),
    horizon_days: int = Form(...),
    manager_consecutive_days_off: int = Form(...),
    db: Session = Depends(get_db),
):
    s = db.get(Settings, 1)
    s.season = season
    s.start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    s.horizon_days = horizon_days
    s.manager_consecutive_days_off = manager_consecutive_days_off
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, db: Session = Depends(get_db)):
    emps = db.query(Employee).order_by(Employee.name).all()
    return templates.TemplateResponse("pages/employees.html", {"request": request, "employees": emps, **_sidebar_context(db)})


@app.post("/employees")
def create_employee(
    name: str = Form(...),
    role: str = Form(...),
    leadership_score: float = Form(0),
    active: bool = Form(False),
    db: Session = Depends(get_db),
):
    db.add(Employee(name=name, role=role, leadership_score=leadership_score, active=active))
    db.commit()
    return RedirectResponse("/employees", status_code=303)


@app.post("/employees/{employee_id}/delete")
def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id)
    if emp:
        db.delete(emp)
        db.commit()
    return RedirectResponse("/employees", status_code=303)


@app.get("/availability", response_class=HTMLResponse)
def availability_page(request: Request, db: Session = Depends(get_db)):
    emps = db.query(Employee).all()
    rows = db.query(Availability).all()
    index = {(r.employee_id, r.day_of_week, r.block): r.available for r in rows}
    return templates.TemplateResponse("pages/availability.html", {"request": request, "employees": emps, "index": index, "blocks": BLOCKS, **_sidebar_context(db)})


@app.post("/availability")
async def save_availability(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    db.query(Availability).delete()
    for emp in db.query(Employee).all():
        for d in range(7):
            for b in BLOCKS:
                key = f"a_{emp.id}_{d}_{b}"
                db.add(Availability(employee_id=emp.id, day_of_week=d, block=b, available=key in form))
    db.commit()
    return RedirectResponse("/availability", status_code=303)


@app.get("/time-off", response_class=HTMLResponse)
def time_off_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("pages/time_off.html", {"request": request, "employees": db.query(Employee).all(), "rows": db.query(TimeOff).order_by(TimeOff.date.desc()).all(), **_sidebar_context(db)})


@app.post("/time-off")
def create_time_off(employee_id: int = Form(...), date_value: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    db.add(TimeOff(employee_id=employee_id, date=datetime.strptime(date_value, "%Y-%m-%d").date(), note=note))
    db.commit()
    return RedirectResponse("/time-off", status_code=303)


@app.post("/generate")
def run_generate(lock_previous: bool = Form(False), db: Session = Depends(get_db)):
    generate_run(db, lock_previous=lock_previous)
    return RedirectResponse("/review", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, employee_id: int | None = None, db: Session = Depends(get_db)):
    run = db.query(Run).order_by(Run.id.desc()).first()
    assignments = []
    violations = []
    if run:
        q = db.query(Assignment).filter(Assignment.run_id == run.id)
        if employee_id:
            q = q.filter(Assignment.employee_id == employee_id)
        assignments = q.order_by(Assignment.date, Assignment.block).all()
        violations = db.query(Violation).filter(Violation.run_id == run.id).order_by(Violation.date).all()
    return templates.TemplateResponse("pages/review.html", {"request": request, "run": run, "assignments": assignments, "violations": violations, "employees": db.query(Employee).all(), **_sidebar_context(db)})


@app.post("/assignments/{assignment_id}/lock")
def lock_assignment(assignment_id: int, db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise HTTPException(404)
    assignment.locked = not assignment.locked
    db.commit()
    return RedirectResponse("/review", status_code=303)


@app.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("pages/exports.html", {"request": request, **_sidebar_context(db)})


@app.get("/exports/latest.json")
def export_json(db: Session = Depends(get_db)):
    run = db.query(Run).order_by(Run.id.desc()).first()
    if not run:
        raise HTTPException(404, "No run available")
    payload = [
        {"date": a.date.isoformat(), "block": a.block, "employee": a.employee.name, "locked": a.locked}
        for a in run.assignments
    ]
    return Response(content=json.dumps(payload, indent=2), media_type="application/json")


@app.get("/exports/latest.csv")
def export_csv(db: Session = Depends(get_db)):
    run = db.query(Run).order_by(Run.id.desc()).first()
    if not run:
        raise HTTPException(404, "No run available")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "block", "employee", "locked"])
    for a in run.assignments:
        writer.writerow([a.date.isoformat(), a.block, a.employee.name, a.locked])
    return Response(content=output.getvalue(), media_type="text/csv")


@app.get("/print", response_class=HTMLResponse)
def print_view(request: Request, db: Session = Depends(get_db)):
    run = db.query(Run).order_by(Run.id.desc()).first()
    assignments = run.assignments if run else []
    return templates.TemplateResponse("pages/print.html", {"request": request, "run": run, "assignments": sorted(assignments, key=lambda x: (x.date, x.block))})
