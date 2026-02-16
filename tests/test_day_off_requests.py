from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app, _sample_payload_dict

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


def all_day_availability() -> dict[str, list[str]]:
    return {day: ["08:30-17:30"] for day in DAY_KEYS}


def build_roster() -> list[dict]:
    return [
        {
            "id": "manager_1",
            "name": "Manager One",
            "role": "Store Manager",
            "min_hours_per_week": 24,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "student": False,
            "availability": all_day_availability(),
        },
        {
            "id": "leader_1",
            "name": "Leader One",
            "role": "Team Leader",
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "student": False,
            "availability": all_day_availability(),
        },
        {
            "id": "clerk_1",
            "name": "Clerk One",
            "role": "Store Clerk",
            "min_hours_per_week": 16,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "student": False,
            "availability": all_day_availability(),
        },
        {
            "id": "captain_1",
            "name": "Captain One",
            "role": "Boat Captain",
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "student": False,
            "availability": all_day_availability(),
        },
    ]


def seed_roster(client: TestClient) -> list[dict]:
    roster = build_roster()
    put = client.put("/api/employees", json=roster)
    assert put.status_code == 200
    return put.json()


def next_sunday_on_or_after(value: date) -> date:
    current = value
    while current.weekday() != 6:
        current += timedelta(days=1)
    return current


def test_admin_user_links_are_optional_but_unique_per_employee():
    client = TestClient(app)
    bootstrap = bootstrap_admin(client)
    assert bootstrap.status_code == 201
    seed_roster(client)

    first = client.post(
        "/api/admin/users",
        json={
            "email": "linked-manager@example.com",
            "temporary_password": "linked-manager-password-123",
            "role": "manager",
            "linked_employee_id": "manager_1",
        },
    )
    assert first.status_code == 201
    assert first.json()["linked_employee_id"] == "manager_1"

    duplicate = client.post(
        "/api/admin/users",
        json={
            "email": "duplicate-manager@example.com",
            "temporary_password": "duplicate-manager-password-123",
            "role": "manager",
            "linked_employee_id": "manager_1",
        },
    )
    assert duplicate.status_code == 409

    second = client.post(
        "/api/admin/users",
        json={
            "email": "unlinked-viewer@example.com",
            "temporary_password": "unlinked-viewer-password-123",
            "role": "view_only",
        },
    )
    assert second.status_code == 201
    assert second.json()["linked_employee_id"] is None

    linked = client.patch(
        f"/api/admin/users/{second.json()['id']}",
        json={"linked_employee_id": "clerk_1"},
    )
    assert linked.status_code == 200
    assert linked.json()["linked_employee_id"] == "clerk_1"

    unlinked = client.patch(
        f"/api/admin/users/{second.json()['id']}",
        json={"linked_employee_id": None},
    )
    assert unlinked.status_code == 200
    assert unlinked.json()["linked_employee_id"] is None


def test_admin_reject_requires_reason_and_can_reverse_to_approve():
    client = TestClient(app)
    bootstrap = bootstrap_admin(client)
    assert bootstrap.status_code == 201
    seed_roster(client)

    created = client.post(
        "/api/admin/users",
        json={
            "email": "manager@example.com",
            "temporary_password": "manager-password-123",
            "role": "manager",
            "linked_employee_id": "manager_1",
        },
    )
    assert created.status_code == 201

    client.post("/auth/logout")
    manager_login = login(client, "manager@example.com", "manager-password-123")
    assert manager_login.status_code == 200
    assert change_password(client, "manager-password-123", "manager-password-456").status_code == 200

    start_date = date.today() + timedelta(days=16)
    end_date = start_date + timedelta(days=2)
    requested = client.post(
        "/api/day-off-requests/me",
        json={"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "reason": "Family trip"},
    )
    assert requested.status_code == 201
    request_id = requested.json()["id"]
    assert requested.json()["employee_id"] == "manager_1"
    assert requested.json()["status"] == "pending"

    client.post("/auth/logout")
    assert login(client, "admin@example.com", "admin-password-123").status_code == 200

    reject_without_reason = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "reject", "reason": ""},
    )
    assert reject_without_reason.status_code == 400

    rejected = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "reject", "reason": "Peak weekend coverage required"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_reason"] == "Peak weekend coverage required"

    approved = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "approve", "reason": "Coverage updated"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    approved_entries = client.get(
        f"/api/day-off-requests/approved?start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
    )
    assert approved_entries.status_code == 200
    assert len(approved_entries.json()) == 3

    re_rejected = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "reject", "reason": "Reopened and denied"},
    )
    assert re_rejected.status_code == 200
    assert re_rejected.json()["status"] == "rejected"

    approved_after_reject = client.get(
        f"/api/day-off-requests/approved?start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
    )
    assert approved_after_reject.status_code == 200
    assert approved_after_reject.json() == []

    re_approved = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "approve", "reason": "Coverage changed again"},
    )
    assert re_approved.status_code == 200
    assert re_approved.json()["status"] == "approved"

    client.post("/auth/logout")
    assert login(client, "manager@example.com", "manager-password-456").status_code == 200
    mine = client.get("/api/day-off-requests/me")
    assert mine.status_code == 200
    assert mine.json()[0]["status"] == "approved"
    assert mine.json()[0]["decision_reason"] == "Coverage changed again"

    cancelled = client.post(f"/api/day-off-requests/me/{request_id}/cancel", json={"reason": ""})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    approved_after_cancel = client.get(
        f"/api/day-off-requests/approved?start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
    )
    assert approved_after_cancel.status_code == 200
    assert approved_after_cancel.json() == []


def test_approved_request_cannot_be_cancelled_after_schedule_exists():
    client = TestClient(app)
    bootstrap = bootstrap_admin(client)
    assert bootstrap.status_code == 201
    roster = seed_roster(client)

    created = client.post(
        "/api/admin/users",
        json={
            "email": "manager@example.com",
            "temporary_password": "manager-password-123",
            "role": "manager",
            "linked_employee_id": "manager_1",
        },
    )
    assert created.status_code == 201

    client.post("/auth/logout")
    manager_login = login(client, "manager@example.com", "manager-password-123")
    assert manager_login.status_code == 200
    assert change_password(client, "manager-password-123", "manager-password-456").status_code == 200

    schedule_start = next_sunday_on_or_after(date.today() + timedelta(days=21))
    locked_date = schedule_start + timedelta(days=2)
    rejected_date = schedule_start + timedelta(days=4)
    cancelled_date = schedule_start + timedelta(days=5)

    requested = client.post(
        "/api/day-off-requests/me",
        json={"start_date": locked_date.isoformat(), "end_date": locked_date.isoformat(), "reason": "Appointment"},
    )
    assert requested.status_code == 201
    request_id = requested.json()["id"]
    rejected_requested = client.post(
        "/api/day-off-requests/me",
        json={"start_date": rejected_date.isoformat(), "end_date": rejected_date.isoformat(), "reason": "Training"},
    )
    assert rejected_requested.status_code == 201
    rejected_request_id = rejected_requested.json()["id"]
    cancelled_requested = client.post(
        "/api/day-off-requests/me",
        json={"start_date": cancelled_date.isoformat(), "end_date": cancelled_date.isoformat(), "reason": "Errand"},
    )
    assert cancelled_requested.status_code == 201
    cancelled_request_id = cancelled_requested.json()["id"]
    cancelled_before_finalize = client.post(f"/api/day-off-requests/me/{cancelled_request_id}/cancel", json={"reason": ""})
    assert cancelled_before_finalize.status_code == 200
    assert cancelled_before_finalize.json()["status"] == "cancelled"

    client.post("/auth/logout")
    assert login(client, "admin@example.com", "admin-password-123").status_code == 200
    approved = client.post(
        f"/api/admin/day-off-requests/{request_id}/decision",
        json={"action": "approve", "reason": ""},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    rejected = client.post(
        f"/api/admin/day-off-requests/{rejected_request_id}/decision",
        json={"action": "reject", "reason": "Coverage required"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = schedule_start.isoformat()
    payload["employees"] = roster
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Locking schedule",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    loaded_as_admin = client.get(f"/api/schedules/{schedule_id}")
    assert loaded_as_admin.status_code == 200
    admin_history_rows = loaded_as_admin.json().get("day_off_requests", [])
    assert any(
        row["id"] == request_id
        and row["requester_email"] == "manager@example.com"
        and row["status"] == "approved"
        for row in admin_history_rows
    )
    assert all(row["id"] != rejected_request_id for row in admin_history_rows)
    assert all(row["id"] != cancelled_request_id for row in admin_history_rows)

    approved_after_schedule = client.get(
        f"/api/day-off-requests/approved?start_date={locked_date.isoformat()}&end_date={locked_date.isoformat()}"
    )
    assert approved_after_schedule.status_code == 200
    assert approved_after_schedule.json() == []

    client.post("/auth/logout")
    assert login(client, "manager@example.com", "manager-password-456").status_code == 200

    loaded_as_manager = client.get(f"/api/schedules/{schedule_id}")
    assert loaded_as_manager.status_code == 200
    manager_history_rows = loaded_as_manager.json().get("day_off_requests", [])
    assert any(row["id"] == request_id and row["status"] == "approved" for row in manager_history_rows)
    assert all(row["id"] != rejected_request_id for row in manager_history_rows)
    assert all(row["id"] != cancelled_request_id for row in manager_history_rows)

    cancel_after_lock = client.post(f"/api/day-off-requests/me/{request_id}/cancel", json={"reason": ""})
    assert cancel_after_lock.status_code == 409

    blocked_new_request = client.post(
        "/api/day-off-requests/me",
        json={"start_date": locked_date.isoformat(), "end_date": locked_date.isoformat(), "reason": ""},
    )
    assert blocked_new_request.status_code == 409


def test_view_only_can_request_for_self_when_linked_and_notice_rule_applies():
    client = TestClient(app)
    bootstrap = bootstrap_admin(client)
    assert bootstrap.status_code == 201
    seed_roster(client)

    created = client.post(
        "/api/admin/users",
        json={
            "email": "viewer@example.com",
            "temporary_password": "viewer-password-123",
            "role": "view_only",
            "linked_employee_id": "clerk_1",
        },
    )
    assert created.status_code == 201

    client.post("/auth/logout")
    viewer_login = login(client, "viewer@example.com", "viewer-password-123")
    assert viewer_login.status_code == 200
    assert change_password(client, "viewer-password-123", "viewer-password-456").status_code == 200

    too_soon = client.post(
        "/api/day-off-requests/me",
        json={
            "start_date": (date.today() + timedelta(days=7)).isoformat(),
            "end_date": (date.today() + timedelta(days=8)).isoformat(),
            "reason": "Too soon",
        },
    )
    assert too_soon.status_code == 400

    accepted = client.post(
        "/api/day-off-requests/me",
        json={
            "start_date": (date.today() + timedelta(days=18)).isoformat(),
            "end_date": (date.today() + timedelta(days=19)).isoformat(),
            "reason": "",
        },
    )
    assert accepted.status_code == 201
    assert accepted.json()["employee_id"] == "clerk_1"
    assert accepted.json()["status"] == "pending"
