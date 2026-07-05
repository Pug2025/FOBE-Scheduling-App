from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.models import ScheduleRun

BOOTSTRAP_TOKEN = "test-bootstrap-token"


def bootstrap_admin(client: TestClient, email: str = "admin@example.com", password: str = "admin-password-123"):
    return client.post(
        "/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": email, "password": password},
    )


def make_viewer_client(admin_client: TestClient, email: str = "viewer@example.com") -> TestClient:
    created = admin_client.post(
        "/api/admin/users",
        json={"email": email, "temporary_password": "temp-password-123", "role": "view_only"},
    )
    assert created.status_code == 201, created.text
    viewer = TestClient(app)
    assert viewer.post("/auth/login", json={"email": email, "password": "temp-password-123"}).status_code == 200
    assert viewer.post(
        "/auth/change-password",
        json={"current_password": "temp-password-123", "new_password": "new-password-456"},
    ).status_code == 200
    return viewer


def save_schedule(client: TestClient, *, period_start: date, weeks: int, label: str) -> int:
    assignments = [
        {
            "date": period_start.isoformat(),
            "location": "Greystones",
            "start": "08:30",
            "end": "17:30",
            "employee_id": "clerk_1",
            "employee_name": "Clerk One",
            "role": "Store Clerk",
            "source": "generated",
        }
    ]
    saved = client.post(
        "/api/schedules",
        json={
            "label": label,
            "period_start": period_start.isoformat(),
            "weeks": weeks,
            "payload_json": {"period": {"start_date": period_start.isoformat(), "weeks": weeks}},
            "result_json": {"assignments": assignments, "totals_by_employee": {}, "violations": []},
        },
    )
    assert saved.status_code == 201, saved.text
    return saved.json()["id"]


def freeze_today(monkeypatch, today: date):
    monkeypatch.setattr(
        main,
        "local_now",
        lambda now=None: main.utc_to_local(datetime(today.year, today.month, today.day, 16, 0, tzinfo=timezone.utc)),
    )


def make_run(run_id: int, period_start: date, weeks: int, created_at: datetime) -> ScheduleRun:
    run = ScheduleRun(
        created_by_user_id=1,
        period_start=period_start,
        weeks=weeks,
        label=f"run-{run_id}",
        payload_json={},
        result_json={"assignments": []},
    )
    run.id = run_id
    run.created_at = created_at
    return run


def test_old_schedule_stays_visible_until_last_day(monkeypatch):
    admin = TestClient(app)
    assert bootstrap_admin(admin).status_code == 201
    old_id = save_schedule(admin, period_start=date(2026, 6, 21), weeks=2, label="old")
    new_id = save_schedule(admin, period_start=date(2026, 7, 5), weeks=2, label="new")
    viewer = make_viewer_client(admin)

    freeze_today(monkeypatch, date(2026, 7, 1))
    body = viewer.get("/api/view-only/schedules").json()
    assert [item["id"] for item in body] == [new_id, old_id]
    old_item = body[1]
    assert old_item["superseded_dates"] == []

    # The day after the old schedule's last day it drops out.
    freeze_today(monkeypatch, date(2026, 7, 5))
    body = viewer.get("/api/view-only/schedules").json()
    assert [item["id"] for item in body] == [new_id]


def test_owner_scenario_overlapping_saturday_is_superseded(monkeypatch):
    admin = TestClient(app)
    assert bootstrap_admin(admin).status_code == 201
    # Old 2-week schedule ends Saturday 2026-07-04.
    old_id = save_schedule(admin, period_start=date(2026, 6, 21), weeks=2, label="old")
    # New 2-week schedule starting that Saturday, saved on the preceding Thursday.
    new_id = save_schedule(admin, period_start=date(2026, 7, 4), weeks=2, label="new")
    viewer = make_viewer_client(admin)

    freeze_today(monkeypatch, date(2026, 7, 2))
    body = viewer.get("/api/view-only/schedules").json()
    assert [item["id"] for item in body] == [new_id, old_id]
    old_item = body[1]
    assert "2026-07-04" in old_item["superseded_dates"]
    assert "2026-07-03" not in old_item["superseded_dates"]
    new_item = body[0]
    assert new_item["superseded_dates"] == []


def test_resaved_period_fully_supersedes_older_duplicate(monkeypatch):
    admin = TestClient(app)
    assert bootstrap_admin(admin).status_code == 201
    active_id = save_schedule(admin, period_start=date(2026, 6, 21), weeks=2, label="active")
    first_save = save_schedule(admin, period_start=date(2026, 7, 5), weeks=2, label="first save")
    second_save = save_schedule(admin, period_start=date(2026, 7, 5), weeks=2, label="second save")
    viewer = make_viewer_client(admin)

    freeze_today(monkeypatch, date(2026, 7, 1))
    body = viewer.get("/api/view-only/schedules").json()
    assert [item["id"] for item in body] == [second_save, active_id]
    assert first_save not in {item["id"] for item in body}


def test_fallback_returns_newest_run_when_none_in_effect(monkeypatch):
    admin = TestClient(app)
    assert bootstrap_admin(admin).status_code == 201
    save_schedule(admin, period_start=date(2026, 5, 3), weeks=2, label="older past")
    newest_id = save_schedule(admin, period_start=date(2026, 5, 17), weeks=2, label="newest past")
    viewer = make_viewer_client(admin)

    freeze_today(monkeypatch, date(2026, 7, 1))
    body = viewer.get("/api/view-only/schedules").json()
    assert [item["id"] for item in body] == [newest_id]
    assert body[0]["superseded_dates"] == []


def test_selection_helper_breaks_created_at_ties_by_id_desc():
    created = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    runs = [
        make_run(1, date(2026, 7, 5), 2, created),
        make_run(2, date(2026, 7, 5), 2, created),
    ]
    selected = main._select_visible_schedule_runs(runs, date(2026, 7, 3))
    assert [run.id for run, _ in selected] == [2]

    selected_reversed = main._select_visible_schedule_runs(list(reversed(runs)), date(2026, 7, 3))
    assert [run.id for run, _ in selected_reversed] == [2]


def test_selection_helper_partial_overlap_marks_only_covered_dates():
    old_run = make_run(1, date(2026, 6, 21), 2, datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc))
    new_run = make_run(2, date(2026, 7, 4), 2, datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    selected = main._select_visible_schedule_runs([old_run, new_run], date(2026, 7, 2))
    assert [run.id for run, _ in selected] == [2, 1]
    superseded_by_id = {run.id: dates for run, dates in selected}
    assert superseded_by_id[2] == set()
    assert superseded_by_id[1] == {date(2026, 7, 4)}


def _extract_js_functions(source: str, names: list[str]) -> str:
    chunks = []
    for name in names:
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(source.index("{", start), len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    chunks.append(source[start:index + 1])
                    break
    return "\n".join(chunks)


def test_viewer_slot_labels_reflect_effective_dates():
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    template = Path(__file__).resolve().parents[1] / "templates" / "pages" / "view_only.html"
    js = _extract_js_functions(
        template.read_text(encoding="utf-8"),
        ["isoDate", "schedulePeriodEnd", "periodDates", "scheduleSlotLabel"],
    )
    harness = js + """
const label = (item, today) => scheduleSlotLabel(item, schedulePeriodEnd(item), today);
console.log(JSON.stringify([
  label({period_start: '2026-06-21', weeks: 2, superseded_dates: ['2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04']}, '2026-07-03'),
  label({period_start: '2026-07-01', weeks: 2, superseded_dates: []}, '2026-07-03'),
  label({period_start: '2026-06-21', weeks: 2, superseded_dates: ['2026-07-04']}, '2026-07-02'),
  label({period_start: '2026-07-04', weeks: 2, superseded_dates: []}, '2026-07-02'),
  label({period_start: '2026-05-17', weeks: 2, superseded_dates: []}, '2026-07-01'),
  label({period_start: '2026-06-21', weeks: 2, superseded_dates: ['2026-06-24', '2026-06-25', '2026-06-26', '2026-06-27', '2026-06-28', '2026-06-29', '2026-06-30']}, '2026-06-26'),
]));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [
        "Previous Schedule",
        "Current Schedule",
        "Current Schedule",
        "Upcoming Schedule",
        "Previous Schedule",
        "Upcoming Schedule",
    ]
