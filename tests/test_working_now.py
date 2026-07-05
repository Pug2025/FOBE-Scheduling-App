from __future__ import annotations

from datetime import date, datetime, timezone

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


def seed_linked_users(client: TestClient, users: list[dict]) -> dict[str, int]:
    roster = [roster_entry("manager_1", "Manager One", "Store Manager"), roster_entry("captain_1", "Captain One", "Boat Captain")]
    roster.extend(roster_entry(user["employee_id"], user["employee_name"], user["role"]) for user in users)
    put = client.put("/api/employees", json=roster)
    assert put.status_code == 200
    ids: dict[str, int] = {}
    for user in users:
        created = client.post(
            "/api/admin/users",
            json={
                "email": user["email"],
                "temporary_password": "temporary-password-123",
                "role": "view_only",
                "linked_employee_id": user["employee_id"],
            },
        )
        assert created.status_code == 201
        ids[user["employee_id"]] = created.json()["id"]
    return ids


def save_single_shift_schedule(client: TestClient, *, work_date: date, employee_id: str, employee_name: str, role: str):
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Working now test schedule",
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


def set_pin(client: TestClient, user_id: int, pin: str):
    response = client.post(f"/api/time-clock/staff/{user_id}/pin", json={"pin": pin, "temporary": False})
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


def patch_local_now(monkeypatch, value: datetime):
    monkeypatch.setattr(main, "local_now", lambda now=None: main.utc_to_local(value))


def unlock_kiosk(client: TestClient):
    response = client.post(
        "/api/kiosk/unlock",
        json={"email": "admin@example.com", "password": "admin-password-123", "session_label": "Cash 1"},
    )
    assert response.status_code == 200


def make_role_client(admin_client: TestClient, email: str, role: str) -> TestClient:
    created = admin_client.post(
        "/api/admin/users",
        json={"email": email, "temporary_password": "temp-password-123", "role": role},
    )
    assert created.status_code == 201, created.text
    user_client = TestClient(app)
    assert user_client.post("/auth/login", json={"email": email, "password": "temp-password-123"}).status_code == 200
    assert user_client.post(
        "/auth/change-password",
        json={"current_password": "temp-password-123", "new_password": "new-password-456"},
    ).status_code == 200
    return user_client


def test_working_now_returns_open_records_only(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_ids = seed_linked_users(
        admin_client,
        [
            {"employee_id": "clerk_a", "employee_name": "Clerk A", "role": "Store Clerk", "email": "clerk-a@example.com"},
            {"employee_id": "clerk_b", "employee_name": "Clerk B", "role": "Store Clerk", "email": "clerk-b@example.com"},
        ],
    )
    set_pin(admin_client, user_ids["clerk_a"], "1234")
    set_pin(admin_client, user_ids["clerk_b"], "5678")
    work_date = date(2026, 7, 15)
    save_single_shift_schedule(admin_client, work_date=work_date, employee_id="clerk_a", employee_name="Clerk A", role="Store Clerk")

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    patch_utcnow(monkeypatch, datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200
    assert kiosk.post("/api/kiosk/clock", json={"pin": "5678"}).status_code == 200
    patch_utcnow(monkeypatch, datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc))
    assert kiosk.post("/api/kiosk/clock", json={"pin": "5678"}).status_code == 200

    review = TestClient(app)
    assert review.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200
    patch_local_now(monkeypatch, datetime(2026, 7, 15, 21, 35, tzinfo=timezone.utc))
    listing = review.get("/api/time-clock/working-now")
    assert listing.status_code == 200
    body = listing.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["employee_name"] == "Clerk A"
    assert entry["role"] == "Store Clerk"
    assert entry["work_date"] == "2026-07-15"
    assert entry["since_local"] == "09:00"
    assert entry["scheduled_end"] == "17:30"
    assert entry["stale"] is False


def test_working_now_flags_prior_day_open_record_as_stale_and_sorts_it_last(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_ids = seed_linked_users(
        admin_client,
        [
            {"employee_id": "clerk_old", "employee_name": "Clerk Old", "role": "Store Clerk", "email": "clerk-old@example.com"},
            {"employee_id": "clerk_new", "employee_name": "Clerk New", "role": "Store Clerk", "email": "clerk-new@example.com"},
        ],
    )
    set_pin(admin_client, user_ids["clerk_old"], "1234")
    set_pin(admin_client, user_ids["clerk_new"], "5678")

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    patch_utcnow(monkeypatch, datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc))
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200
    patch_utcnow(monkeypatch, datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc))
    assert kiosk.post("/api/kiosk/clock", json={"pin": "5678"}).status_code == 200

    review = TestClient(app)
    assert review.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200
    patch_local_now(monkeypatch, datetime(2026, 7, 11, 14, 0, tzinfo=timezone.utc))
    listing = review.get("/api/time-clock/working-now")
    assert listing.status_code == 200
    body = listing.json()
    assert [entry["employee_name"] for entry in body] == ["Clerk New", "Clerk Old"]
    assert body[0]["stale"] is False
    assert body[1]["stale"] is True
    assert body[1]["work_date"] == "2026-07-10"


def test_working_now_accessible_to_managers():
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    manager_client = make_role_client(admin_client, "mgr@example.com", "manager")
    listing = manager_client.get("/api/time-clock/working-now")
    assert listing.status_code == 200
    assert listing.json() == []


def test_working_now_forbidden_for_non_manager_roles():
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    for role in ("view_only", "user", "report_only"):
        role_client = make_role_client(admin_client, f"{role.replace('_', '-')}@example.com", role)
        assert role_client.get("/api/time-clock/working-now").status_code == 403


def test_working_now_requires_authentication():
    anon = TestClient(app)
    assert anon.get("/api/time-clock/working-now").status_code == 401
