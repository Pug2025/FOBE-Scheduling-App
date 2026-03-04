from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.main import app

BOOTSTRAP_TOKEN = "test-bootstrap-token"
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def bootstrap_admin(client: TestClient, email: str = "admin@example.com", password: str = "admin-password-123"):
    return client.post(
        "/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": email, "password": password},
    )


def login(client: TestClient, email: str, password: str):
    return client.post("/auth/login", json={"email": email, "password": password})


def change_password(client: TestClient, current_password: str, new_password: str):
    return client.post(
        "/auth/change-password",
        json={"current_password": current_password, "new_password": new_password},
    )


def roster_entry(employee_id: str, name: str, role: str) -> dict:
    return {
        "id": employee_id,
        "name": name,
        "role": role,
        "min_hours_per_week": 0,
        "max_hours_per_week": 40,
        "priority_tier": "A",
        "student": False,
        "availability": {day: ["08:30-17:30"] for day in DAY_KEYS},
    }


def seed_linked_user(client: TestClient, *, employee_id: str, employee_name: str, role: str, email: str) -> int:
    roster = [
        roster_entry("manager_1", "Manager One", "Store Manager"),
        roster_entry(employee_id, employee_name, role),
    ]
    assert client.put("/api/employees", json=roster).status_code == 200
    created = client.post(
        "/api/admin/users",
        json={
            "email": email,
            "temporary_password": "temporary-password-123",
            "role": "view_only",
            "linked_employee_id": employee_id,
        },
    )
    assert created.status_code == 201
    return created.json()["id"]


def save_single_shift_schedule(client: TestClient, *, work_date: date, employee_id: str, employee_name: str, role: str):
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Surface smoke schedule",
            "period_start": work_date.isoformat(),
            "weeks": 1,
            "payload_json": {"period": {"start_date": work_date.isoformat(), "weeks": 1}},
            "result_json": {
                "assignments": [
                    {
                        "date": work_date.isoformat(),
                        "location": "Greystones",
                        "start": "08:30",
                        "end": "17:30",
                        "employee_id": employee_id,
                        "employee_name": employee_name,
                        "role": role,
                        "source": "generated",
                    }
                ],
                "totals_by_employee": {},
                "violations": [],
            },
        },
    )
    assert saved.status_code == 201


def set_pin(client: TestClient, user_id: int, pin: str = "1234"):
    response = client.post(f"/api/time-clock/staff/{user_id}/pin", json={"pin": pin, "temporary": False})
    assert response.status_code == 200


def unlock_kiosk(client: TestClient):
    response = client.post(
        "/api/kiosk/unlock",
        json={"email": "admin@example.com", "password": "admin-password-123", "session_label": "Smoke Test"},
    )
    assert response.status_code == 200


def patch_utcnow(monkeypatch, *values: datetime):
    iterator = iter(values)
    last_value = values[-1]

    def fake_utcnow() -> datetime:
        nonlocal iterator
        try:
            return next(iterator)
        except StopIteration:
            return last_value

    monkeypatch.setattr(main, "utcnow", fake_utcnow)


def test_public_surfaces_and_locked_kiosk_status_render():
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    root = client.get("/")
    assert root.status_code == 200
    assert "payload_json" in root.text

    kiosk = client.get("/kiosk")
    assert kiosk.status_code == 200
    assert "Clock In / Out" in kiosk.text
    assert "Unlock" in kiosk.text

    settings = client.post("/settings", follow_redirects=False)
    assert settings.status_code == 303
    assert settings.headers["location"] == "/"

    kiosk_status = client.get("/api/kiosk/status")
    assert kiosk_status.status_code == 200
    assert kiosk_status.json() == {"unlocked": False, "unlocked_by_email": None, "expires_at": None, "session_label": None}



def test_role_based_dashboard_pages_render_correctly():
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    seed_linked_user(
        admin_client,
        employee_id="viewer_1",
        employee_name="Viewer One",
        role="Store Clerk",
        email="viewer@example.com",
    )

    admin_time_clock = admin_client.get("/time-clock")
    assert admin_time_clock.status_code == 200
    assert "Time Clock" in admin_time_clock.text

    admin_viewer = admin_client.get("/viewer", follow_redirects=False)
    assert admin_viewer.status_code == 303
    assert admin_viewer.headers["location"] == "/"

    viewer_client = TestClient(app)
    assert login(viewer_client, "viewer@example.com", "temporary-password-123").status_code == 200
    assert change_password(viewer_client, "temporary-password-123", "viewer-password-456").status_code == 200

    redirected_root = viewer_client.get("/", follow_redirects=False)
    assert redirected_root.status_code == 303
    assert redirected_root.headers["location"] == "/viewer"

    viewer_page = viewer_client.get("/viewer")
    assert viewer_page.status_code == 200

    blocked_time_clock = viewer_client.get("/time-clock", follow_redirects=False)
    assert blocked_time_clock.status_code == 303
    assert blocked_time_clock.headers["location"] == "/"



def test_generate_and_schedule_export_surfaces_work():
    client = TestClient(app)
    assert bootstrap_admin(client).status_code == 201

    policy = client.get("/api/time-clock/policy")
    assert policy.status_code == 200
    assert policy.json()["timezone"] == "America/Toronto"

    payload = main._sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    body = generated.json()
    assert body["assignments"]

    export_json = client.get("/export/json")
    assert export_json.status_code == 200
    assert export_json.json()["assignments"]

    export_csv = client.get("/export/csv")
    assert export_csv.status_code == 200
    assert export_csv.text.startswith("date,location,start,end,employee_id,employee_name,role")



def test_time_clock_csv_export_surface_returns_closed_records(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="clerk_export",
        employee_name="Clerk Export",
        role="Store Clerk",
        email="clerk-export@example.com",
    )
    set_pin(admin_client, user_id, pin="1234")
    work_date = date(2026, 7, 24)
    save_single_shift_schedule(
        admin_client,
        work_date=work_date,
        employee_id="clerk_export",
        employee_name="Clerk Export",
        role="Store Clerk",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 12, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 12, 3, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 12, 4, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 21, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 21, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 21, 26, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 21, 27, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200

    review_client = TestClient(app)
    assert login(review_client, "admin@example.com", "admin-password-123").status_code == 200

    export = review_client.get(
        "/api/time-clock/export.csv",
        params={"start_date": work_date.isoformat(), "end_date": work_date.isoformat()},
    )
    assert export.status_code == 200
    assert export.text.startswith(
        "work_date,employee_name,role,scheduled_start,scheduled_end,scheduled_paid_hours,effective_clock_in,effective_clock_out,payable_hours,break_deduction_minutes,review_state,review_note"
    )
    assert "Clerk Export" in export.text
