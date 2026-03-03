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


def seed_linked_user(client: TestClient, *, employee_id: str, employee_name: str, role: str, email: str) -> int:
    roster = [
        roster_entry("manager_1", "Manager One", "Store Manager"),
        roster_entry(employee_id, employee_name, role),
        roster_entry("captain_1", "Captain One", "Boat Captain"),
    ]
    put = client.put("/api/employees", json=roster)
    assert put.status_code == 200
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


def save_single_shift_schedule(
    client: TestClient,
    *,
    work_date: date,
    employee_id: str,
    employee_name: str,
    role: str,
    start: str = "08:30",
    end: str = "17:30",
    location: str = "Greystones",
):
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Time clock test schedule",
            "period_start": work_date.isoformat(),
            "weeks": 1,
            "payload_json": {"period": {"start_date": work_date.isoformat(), "weeks": 1}},
            "result_json": {
                "assignments": [
                    {
                        "date": work_date.isoformat(),
                        "location": location,
                        "start": start,
                        "end": end,
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


def set_pin(client: TestClient, user_id: int, pin: str = "1234", *, temporary: bool = False):
    response = client.post(f"/api/time-clock/staff/{user_id}/pin", json={"pin": pin, "temporary": temporary})
    assert response.status_code == 200
    return response.json()


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


def unlock_kiosk(client: TestClient):
    response = client.post(
        "/api/kiosk/unlock",
        json={"email": "admin@example.com", "password": "admin-password-123", "session_label": "Cash 1"},
    )
    assert response.status_code == 200
    return response


def test_break_policy_short_and_medium_shift_hours():
    assert main._hours_between("08:30", "13:30") == 5.0
    assert main._hours_between("08:30", "14:30") == 5.5
    assert main._hours_between("08:30", "17:30") == 8.0


def test_kiosk_unlock_uses_separate_session_and_defaults_to_scheduled_hours(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="clerk_1",
        employee_name="Clerk One",
        role="Store Clerk",
        email="clerk@example.com",
    )
    set_pin(admin_client, user_id, temporary=False)
    work_date = date(2026, 7, 15)
    save_single_shift_schedule(
        admin_client,
        work_date=work_date,
        employee_id="clerk_1",
        employee_name="Clerk One",
        role="Store Clerk",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 12, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 12, 33, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 12, 34, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 21, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 21, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 21, 26, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 21, 27, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    assert kiosk.get("/auth/me").status_code == 401

    clock_in = kiosk.post("/api/kiosk/clock", json={"pin": "1234"})
    assert clock_in.status_code == 200
    assert clock_in.json()["action"] == "clocked_in"

    clock_out = kiosk.post("/api/kiosk/clock", json={"pin": "1234"})
    assert clock_out.status_code == 200
    record = clock_out.json()["record"]
    assert record["used_scheduled_default"] is True
    assert record["payable_hours"] == 8.0
    assert record["review_state"] == "clear"


def test_temporary_pin_requires_reset_before_first_punch(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="clerk_temp",
        employee_name="Temp Clerk",
        role="Store Clerk",
        email="temp-clerk@example.com",
    )
    set_pin(admin_client, user_id, pin="1234", temporary=True)

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 16, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 7, 16, 12, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 16, 12, 33, tzinfo=timezone.utc),
        datetime(2026, 7, 16, 12, 34, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)

    first = kiosk.post("/api/kiosk/clock", json={"pin": "1234"})
    assert first.status_code == 200
    assert first.json()["action"] == "pin_change_required"
    assert first.json()["record"] is None
    assert first.json()["employee_name"] == "Temp Clerk"

    second = kiosk.post(
        "/api/kiosk/clock",
        json={"pin": "1234", "new_pin": "5678", "confirm_new_pin": "5678"},
    )
    assert second.status_code == 200
    assert second.json()["action"] == "clocked_in"

    review_client = TestClient(app)
    assert review_client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200
    staff = review_client.get("/api/time-clock/staff")
    assert staff.status_code == 200
    target = next(row for row in staff.json() if row["user_id"] == user_id)
    assert target["pin_status"] == "active"
    assert target["pin_temporary"] is False


def test_only_significant_employee_time_change_needs_approval(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_ids = seed_linked_users(
        admin_client,
        [
            {
                "employee_id": "clerk_small",
                "employee_name": "Clerk Small",
                "role": "Store Clerk",
                "email": "clerk-small@example.com",
            },
            {
                "employee_id": "clerk_large",
                "employee_name": "Clerk Large",
                "role": "Store Clerk",
                "email": "clerk-large@example.com",
            },
        ],
    )
    small_user_id = user_ids["clerk_small"]
    large_user_id = user_ids["clerk_large"]
    set_pin(admin_client, small_user_id, pin="1234", temporary=False)
    set_pin(admin_client, large_user_id, pin="5678", temporary=False)
    work_date = date(2026, 7, 17)
    save_single_shift_schedule(admin_client, work_date=work_date, employee_id="clerk_small", employee_name="Clerk Small", role="Store Clerk")
    save_single_shift_schedule(admin_client, work_date=work_date, employee_id="clerk_large", employee_name="Clerk Large", role="Store Clerk")

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 33, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 34, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 26, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 27, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 40, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 41, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 42, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 43, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 12, 44, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 28, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 29, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 21, 31, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)

    small_in = kiosk.post(
        "/api/kiosk/clock",
        json={"pin": "1234", "override_time": "08:00", "override_reason": "Bakery pickup"},
    )
    assert small_in.status_code == 200
    small_out = kiosk.post("/api/kiosk/clock", json={"pin": "1234"})
    assert small_out.status_code == 200
    assert small_out.json()["record"]["review_state"] == "clear"

    large_in = kiosk.post(
        "/api/kiosk/clock",
        json={"pin": "5678", "override_time": "07:00", "override_reason": "Extended supply run"},
    )
    assert large_in.status_code == 200
    large_out = kiosk.post("/api/kiosk/clock", json={"pin": "5678"})
    assert large_out.status_code == 200
    assert large_out.json()["record"]["review_state"] == "needs_review"


def test_manager_can_approve_significant_employee_change(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="clerk_review",
        employee_name="Clerk Review",
        role="Store Clerk",
        email="clerk-review@example.com",
    )
    set_pin(admin_client, user_id, pin="1234", temporary=False)
    work_date = date(2026, 7, 18)
    save_single_shift_schedule(
        admin_client,
        work_date=work_date,
        employee_id="clerk_review",
        employee_name="Clerk Review",
        role="Store Clerk",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 12, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 12, 33, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 12, 34, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 21, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 21, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 21, 26, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 21, 27, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 21, 28, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    assert kiosk.post(
        "/api/kiosk/clock",
        json={"pin": "1234", "override_time": "07:00", "override_reason": "Extended supply run"},
    ).status_code == 200
    closed = kiosk.post("/api/kiosk/clock", json={"pin": "1234"})
    assert closed.status_code == 200
    record = closed.json()["record"]
    assert record["review_state"] == "needs_review"

    review_client = TestClient(app)
    assert review_client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200

    listing = review_client.get(
        "/api/time-clock/records",
        params={"start_date": work_date.isoformat(), "end_date": work_date.isoformat()},
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    approved = review_client.post(
        f"/api/time-clock/records/{record['id']}/approve",
        json={"note": "Confirmed with store lead"},
    )
    assert approved.status_code == 200
    assert approved.json()["review_state"] == "approved"
    assert approved.json()["review_note"] == "Confirmed with store lead"


def test_captain_swap_schedule_still_uses_captain_rules(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_ids = seed_linked_users(
        admin_client,
        [
            {
                "employee_id": "captain_a",
                "employee_name": "Captain A",
                "role": "Boat Captain",
                "email": "captain-a@example.com",
            },
            {
                "employee_id": "captain_b",
                "employee_name": "Captain B",
                "role": "Boat Captain",
                "email": "captain-b@example.com",
            },
        ],
    )
    set_pin(admin_client, user_ids["captain_b"], pin="2468", temporary=False)
    work_date = date(2026, 7, 20)
    save_single_shift_schedule(
        admin_client,
        work_date=work_date,
        employee_id="captain_a",
        employee_name="Captain A",
        role="Boat Captain",
        location="Boat",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 12, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 12, 3, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 5, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 6, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 7, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 8, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 9, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    clock_in = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert clock_in.status_code == 200
    record = clock_in.json()["record"]
    assert record["effective_clock_in_local"] == "09:00"
    assert record["scheduled_start"] == "08:30"
    assert record["scheduled_end"] == "17:30"
    assert record["review_note"] is None


def test_captain_full_day_auto_closes_at_five_on_records_load(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="captain_auto",
        employee_name="Captain Auto",
        role="Boat Captain",
        email="captain-auto@example.com",
    )
    set_pin(admin_client, user_id, pin="2468", temporary=False)
    work_date = date(2026, 7, 21)
    save_single_shift_schedule(
        admin_client,
        work_date=work_date,
        employee_id="captain_auto",
        employee_name="Captain Auto",
        role="Boat Captain",
        location="Boat",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 12, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 12, 3, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 13, 20, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 13, 21, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 13, 22, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 13, 23, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 13, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 11, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 12, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 13, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 14, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 21, 15, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    clock_in = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert clock_in.status_code == 200
    assert clock_in.json()["record"]["effective_clock_in_local"] == "09:00"

    review_client = TestClient(app)
    assert review_client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200
    monkeypatch.setattr(
        main,
        "local_now",
        lambda now=None: main.utc_to_local(datetime(2026, 7, 21, 21, 15, tzinfo=timezone.utc)),
    )
    records = review_client.get(
        "/api/time-clock/records",
        params={"start_date": work_date.isoformat(), "end_date": work_date.isoformat()},
    )
    assert records.status_code == 200
    body = records.json()
    assert len(body) == 1
    assert body[0]["status"] == "closed"
    assert body[0]["effective_clock_out_local"] == "17:00"
    assert body[0]["last_action_source"] == "auto_close"


def test_captain_manual_clock_out_rules_snap_to_five_and_quarter_hour(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_id = seed_linked_user(
        admin_client,
        employee_id="captain_round",
        employee_name="Captain Round",
        role="Boat Captain",
        email="captain-round@example.com",
    )
    set_pin(admin_client, user_id, pin="2468", temporary=False)
    first_day = date(2026, 7, 22)
    second_day = date(2026, 7, 23)
    save_single_shift_schedule(
        admin_client,
        work_date=first_day,
        employee_id="captain_round",
        employee_name="Captain Round",
        role="Boat Captain",
        location="Boat",
    )
    save_single_shift_schedule(
        admin_client,
        work_date=second_day,
        employee_id="captain_round",
        employee_name="Captain Round",
        role="Boat Captain",
        location="Boat",
    )

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 12, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 12, 3, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 2, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 3, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 4, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 5, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 20, 40, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 20, 41, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 20, 42, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 20, 43, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 13, 8, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 13, 9, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 13, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 13, 11, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 20, 7, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 20, 8, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 20, 9, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 20, 10, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)

    first_in = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert first_in.status_code == 200
    assert first_in.json()["record"]["effective_clock_in_local"] == "09:00"
    first_out = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert first_out.status_code == 200
    assert first_out.json()["record"]["effective_clock_out_local"] == "17:00"

    second_in = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert second_in.status_code == 200
    assert second_in.json()["record"]["effective_clock_in_local"] == "09:00"
    second_out = kiosk.post("/api/kiosk/clock", json={"pin": "2468"})
    assert second_out.status_code == 200
    assert second_out.json()["record"]["effective_clock_out_local"] == "16:00"


def test_pin_must_be_unique_and_finished_timesheet_exports(monkeypatch):
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    user_ids = seed_linked_users(
        admin_client,
        [
            {
                "employee_id": "clerk_a",
                "employee_name": "Clerk A",
                "role": "Store Clerk",
                "email": "clerk-a@example.com",
            },
            {
                "employee_id": "clerk_b",
                "employee_name": "Clerk B",
                "role": "Store Clerk",
                "email": "clerk-b@example.com",
            },
        ],
    )
    first_user_id = user_ids["clerk_a"]
    second_user_id = user_ids["clerk_b"]
    set_pin(admin_client, first_user_id, pin="1234", temporary=False)
    duplicate = admin_client.post(f"/api/time-clock/staff/{second_user_id}/pin", json={"pin": "1234", "temporary": False})
    assert duplicate.status_code == 409

    set_pin(admin_client, second_user_id, pin="5678", temporary=False)
    work_date = date(2026, 7, 19)
    save_single_shift_schedule(admin_client, work_date=work_date, employee_id="clerk_a", employee_name="Clerk A", role="Store Clerk")
    save_single_shift_schedule(admin_client, work_date=work_date, employee_id="clerk_b", employee_name="Clerk B", role="Store Clerk")

    patch_utcnow(
        monkeypatch,
        datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 33, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 34, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 26, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 27, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 35, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 36, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 37, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 12, 38, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 28, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 29, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 19, 21, 31, tzinfo=timezone.utc),
    )

    kiosk = TestClient(app)
    unlock_kiosk(kiosk)
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200
    assert kiosk.post("/api/kiosk/clock", json={"pin": "1234"}).status_code == 200
    assert kiosk.post("/api/kiosk/clock", json={"pin": "5678"}).status_code == 200
    assert kiosk.post("/api/kiosk/clock", json={"pin": "5678"}).status_code == 200

    review_client = TestClient(app)
    assert review_client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    ).status_code == 200

    timesheet = review_client.get(
        "/api/time-clock/timesheet",
        params={"start_date": work_date.isoformat(), "end_date": work_date.isoformat()},
    )
    assert timesheet.status_code == 200
    body = timesheet.json()
    assert len(body["rows"]) == 2
    assert body["grand_total_hours"] == 16.0
    assert {row["employee_name"] for row in body["rows"]} == {"Clerk A", "Clerk B"}

    export = review_client.get(
        "/api/time-clock/timesheet.csv",
        params={"start_date": work_date.isoformat(), "end_date": work_date.isoformat()},
    )
    assert export.status_code == 200
    assert "Clerk A" in export.text
    assert "clerk_a" not in export.text
    assert "clerk_b" not in export.text
    assert "grand_total_hours,16.0" in export.text


def test_time_clock_staff_lists_linked_users_and_delete_removes_employee():
    admin_client = TestClient(app)
    assert bootstrap_admin(admin_client).status_code == 201
    roster = [
        roster_entry("manager_1", "Manager One", "Store Manager"),
        roster_entry("lead_1", "Libby", "Team Leader"),
        roster_entry("clerk_1", "Parker", "Store Clerk"),
        roster_entry("captain_1", "Captain One", "Boat Captain"),
    ]
    assert admin_client.put("/api/employees", json=roster).status_code == 200
    created = admin_client.post(
        "/api/admin/users",
        json={
            "email": "libby@example.com",
            "temporary_password": "temporary-password-123",
            "role": "view_only",
            "linked_employee_id": "lead_1",
        },
    )
    assert created.status_code == 201

    staff = admin_client.get("/api/time-clock/staff")
    assert staff.status_code == 200
    body = staff.json()
    assert [row["employee_name"] for row in body] == ["Libby"]
    assert body[0]["has_linked_account"] is True

    deleted = admin_client.delete("/api/time-clock/staff/lead_1")
    assert deleted.status_code == 200

    roster_after = admin_client.get("/api/employees")
    assert roster_after.status_code == 200
    assert {row["name"] for row in roster_after.json()} == {"Manager One", "Parker", "Captain One"}

    users_after = admin_client.get("/api/admin/users")
    assert users_after.status_code == 200
    former_libby = next(row for row in users_after.json() if row["email"] == "libby@example.com")
    assert former_libby["linked_employee_id"] is None
    assert former_libby["is_active"] is False
