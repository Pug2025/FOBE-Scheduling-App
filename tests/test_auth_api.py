from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.db as app_db
from app.main import app, _sample_payload_dict
from app.models import SessionRecord, User

BOOTSTRAP_TOKEN = "test-bootstrap-token"


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


def test_bootstrap_requires_token_and_only_runs_once():
    client = TestClient(app)

    missing = client.post("/auth/bootstrap", json={"email": "owner@example.com", "password": "strong-password-123"})
    assert missing.status_code == 403

    first = bootstrap_admin(client, "owner@example.com", "strong-password-123")
    assert first.status_code == 201
    assert first.json()["role"] == "admin"

    db = app_db.SessionLocal()
    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None
    assert user.password_hash != "strong-password-123"
    assert user.password_hash.startswith("$2")
    db.close()

    second = bootstrap_admin(client, "second@example.com", "another-password-123")
    assert second.status_code == 409


def test_bootstrap_status_enabled_only_before_first_user():
    client = TestClient(app)

    before = client.get("/auth/bootstrap/status")
    assert before.status_code == 200
    assert before.json() == {"enabled": True}

    first = bootstrap_admin(client, "owner@example.com", "strong-password-123")
    assert first.status_code == 201

    after = client.get("/auth/bootstrap/status")
    assert after.status_code == 200
    assert after.json() == {"enabled": False}


def test_bootstrap_status_disabled_when_token_missing(monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_TOKEN", raising=False)
    client = TestClient(app)

    status = client.get("/auth/bootstrap/status")
    assert status.status_code == 200
    assert status.json() == {"enabled": False}


def test_login_logout_and_me_flow():
    client = TestClient(app)
    bootstrap_admin(client)
    client.post("/auth/logout")

    login_res = login(client, "admin@example.com", "admin-password-123")
    assert login_res.status_code == 200
    cookie = login_res.headers.get("set-cookie", "")
    assert "session_id=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"

    logout = client.post("/auth/logout")
    assert logout.status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_cookie_is_secure_when_forwarded_proto_is_https():
    client = TestClient(app)
    bootstrap_admin(client)
    client.post("/auth/logout")

    login_res = client.post(
        "/auth/login",
        headers={"x-forwarded-proto": "https"},
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert login_res.status_code == 200
    cookie = login_res.headers.get("set-cookie", "")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_auth_and_api_responses_disable_cache():
    client = TestClient(app)
    bootstrap_admin(client)

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert "no-store" in me.headers.get("cache-control", "")
    assert "no-cache" in me.headers.get("pragma", "")

    schedules = client.get("/api/schedules")
    assert schedules.status_code == 200
    assert "no-store" in schedules.headers.get("cache-control", "")
    assert "no-cache" in schedules.headers.get("pragma", "")


def test_session_persists_across_clients_and_expired_sessions_are_rejected():
    client = TestClient(app)
    bootstrap_admin(client)

    session_id = client.cookies.get("session_id")
    assert session_id

    second_client = TestClient(app)
    second_client.cookies.set("session_id", session_id)
    assert second_client.get("/auth/me").status_code == 200

    db = app_db.SessionLocal()
    row = db.get(SessionRecord, session_id)
    assert row is not None
    row.expires_at = row.created_at - timedelta(seconds=1)
    db.add(row)
    db.commit()
    db.close()

    assert second_client.get("/auth/me").status_code == 401


def test_temporary_password_requires_change_before_workspace_access():
    client = TestClient(app)
    bootstrap_admin(client)

    created = client.post(
        "/api/admin/users",
        json={"email": "manager@example.com", "temporary_password": "manager-password-123", "role": "manager"},
    )
    assert created.status_code == 201
    assert created.json()["must_change_password"] is True

    client.post("/auth/logout")
    login_res = login(client, "manager@example.com", "manager-password-123")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True

    blocked = client.get("/api/employees")
    assert blocked.status_code == 403
    assert "Password change required" in blocked.json()["detail"]

    wrong_current = change_password(client, "wrong-password", "manager-password-456")
    assert wrong_current.status_code == 401

    weak_password = change_password(client, "manager-password-123", "too-short")
    assert weak_password.status_code == 400

    changed = change_password(client, "manager-password-123", "manager-password-456")
    assert changed.status_code == 200
    assert changed.json()["must_change_password"] is False

    roster_get = client.get("/api/employees")
    assert roster_get.status_code == 200


def test_admin_reset_to_temporary_password_requires_change_again():
    client = TestClient(app)
    bootstrap_admin(client)

    created = client.post(
        "/api/admin/users",
        json={"email": "manager@example.com", "temporary_password": "manager-password-123", "role": "manager"},
    )
    assert created.status_code == 201
    manager_id = created.json()["id"]

    client.post("/auth/logout")
    assert login(client, "manager@example.com", "manager-password-123").status_code == 200
    assert change_password(client, "manager-password-123", "manager-password-456").status_code == 200
    client.post("/auth/logout")

    assert login(client, "admin@example.com", "admin-password-123").status_code == 200
    reset = client.patch(
        f"/api/admin/users/{manager_id}",
        json={"temporary_password": "manager-password-789"},
    )
    assert reset.status_code == 200
    assert reset.json()["must_change_password"] is True
    client.post("/auth/logout")

    assert login(client, "manager@example.com", "manager-password-456").status_code == 401
    login_res = login(client, "manager@example.com", "manager-password-789")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True
    assert client.get("/api/employees").status_code == 403


def test_manager_permissions_and_payload_driven_generate():
    client = TestClient(app)
    bootstrap_admin(client)

    roster = [
        {
            "id": "mgr",
            "name": "Manager",
            "role": "Store Manager",
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "availability": {k: ["08:30-17:30"] for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        },
        {
            "id": "lead",
            "name": "Leader",
            "role": "Team Leader",
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "A",
            "availability": {k: ["08:30-17:30"] for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        },
        {
            "id": "clerk",
            "name": "Clerk",
            "role": "Store Clerk",
            "min_hours_per_week": 16,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "availability": {k: ["08:30-17:30"] for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        },
        {
            "id": "captain",
            "name": "Captain",
            "role": "Boat Captain",
            "min_hours_per_week": 20,
            "max_hours_per_week": 40,
            "priority_tier": "B",
            "availability": {k: ["08:30-17:30"] for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        },
    ]

    put = client.put("/api/employees", json=roster)
    assert put.status_code == 200
    assert put.json()[0]["id"] == "mgr"

    create_user = client.post(
        "/api/admin/users",
        json={"email": "manager@example.com", "temporary_password": "manager-password-123", "role": "manager"},
    )
    assert create_user.status_code == 201

    client.post("/auth/logout")
    login_res = login(client, "manager@example.com", "manager-password-123")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True
    changed = change_password(client, "manager-password-123", "manager-password-456")
    assert changed.status_code == 200

    roster_get = client.get("/api/employees")
    assert roster_get.status_code == 200
    assert len(roster_get.json()) == 4

    roster_put_allowed = client.put("/api/employees", json=roster)
    assert roster_put_allowed.status_code == 200

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    payload["employees"] = roster_get.json()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    assert len(generated.json()["assignments"]) > 0


def test_admin_endpoints_require_admin_and_disabled_user_cannot_login():
    client = TestClient(app)
    bootstrap_admin(client)

    created = client.post(
        "/api/admin/users",
        json={"email": "staff@example.com", "temporary_password": "staff-password-123", "role": "manager"},
    )
    assert created.status_code == 201
    staff_id = created.json()["id"]

    client.post("/auth/logout")
    assert login(client, "staff@example.com", "staff-password-123").status_code == 200
    assert client.get("/api/admin/users").status_code == 403
    assert client.delete(f"/api/admin/users/{staff_id}").status_code == 403

    client.post("/auth/logout")
    assert login(client, "admin@example.com", "admin-password-123").status_code == 200

    disabled = client.patch(f"/api/admin/users/{staff_id}", json={"is_active": False})
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    client.post("/auth/logout")
    disabled_login = login(client, "staff@example.com", "staff-password-123")
    assert disabled_login.status_code == 403


def test_signed_in_admin_cannot_demote_or_disable_self():
    client = TestClient(app)
    bootstrap_admin(client)

    me = client.get("/auth/me")
    assert me.status_code == 200
    admin_id = me.json()["id"]

    demote = client.patch(f"/api/admin/users/{admin_id}", json={"role": "manager"})
    assert demote.status_code == 400
    assert "own role" in demote.json()["detail"]

    disable = client.patch(f"/api/admin/users/{admin_id}", json={"is_active": False})
    assert disable.status_code == 400
    assert "own account" in disable.json()["detail"]

    password_only = client.patch(
        f"/api/admin/users/{admin_id}",
        json={"temporary_password": "new-admin-password-123"},
    )
    assert password_only.status_code == 200
    assert password_only.json()["role"] == "admin"
    assert password_only.json()["is_active"] is True


def test_admin_can_switch_user_between_manager_and_view_only():
    client = TestClient(app)
    bootstrap_admin(client)

    created = client.post(
        "/api/admin/users",
        json={"email": "switchable@example.com", "temporary_password": "switchable-password-123", "role": "manager"},
    )
    assert created.status_code == 201
    target_id = created.json()["id"]
    assert created.json()["role"] == "manager"

    to_view_only = client.patch(f"/api/admin/users/{target_id}", json={"role": "view_only"})
    assert to_view_only.status_code == 200
    assert to_view_only.json()["role"] == "view_only"

    to_manager = client.patch(f"/api/admin/users/{target_id}", json={"role": "manager"})
    assert to_manager.status_code == 200
    assert to_manager.json()["role"] == "manager"


def test_admin_can_delete_other_user_but_not_self():
    client = TestClient(app)
    bootstrap_admin(client)

    created = client.post(
        "/api/admin/users",
        json={"email": "delete-me@example.com", "temporary_password": "delete-me-password-123", "role": "manager"},
    )
    assert created.status_code == 201
    target_id = created.json()["id"]

    deleted = client.delete(f"/api/admin/users/{target_id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    users_after = client.get("/api/admin/users")
    assert users_after.status_code == 200
    assert all(user["id"] != target_id for user in users_after.json())

    me = client.get("/auth/me")
    assert me.status_code == 200
    self_delete = client.delete(f"/api/admin/users/{me.json()['id']}")
    assert self_delete.status_code == 400
    assert "own account" in self_delete.json()["detail"]


def test_admin_can_save_schedule_and_load_it_back():
    client = TestClient(app)
    bootstrap_admin(client)

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200

    create = client.post(
        "/api/schedules",
        json={
            "label": "My saved schedule",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert create.status_code == 201
    schedule_id = create.json()["id"]

    listing = client.get("/api/schedules")
    assert listing.status_code == 200
    items = listing.json()
    assert any(item["id"] == schedule_id for item in items)
    match = next(item for item in items if item["id"] == schedule_id)
    assert match["created_by_email"] == "admin@example.com"

    fetched = client.get(f"/api/schedules/{schedule_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["payload_json"]["period"]["start_date"] == payload["period"]["start_date"]
    assert body["result_json"]["assignments"] == generated.json()["assignments"]


def test_view_only_cannot_post_saved_schedule():
    client = TestClient(app)
    bootstrap_admin(client)
    create_user = client.post(
        "/api/admin/users",
        json={"email": "viewer@example.com", "temporary_password": "viewer-password-123", "role": "view_only"},
    )
    assert create_user.status_code == 201

    client.post("/auth/logout")
    assert login(client, "viewer@example.com", "viewer-password-123").status_code == 200

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    forbidden = client.post(
        "/api/schedules",
        json={
            "label": "Should fail",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": {"assignments": [], "violations": [], "totals_by_employee": {}},
        },
    )
    assert forbidden.status_code == 403


def test_manager_can_list_and_view_saved_schedules():
    client = TestClient(app)
    bootstrap_admin(client)
    create_user = client.post(
        "/api/admin/users",
        json={"email": "manager@example.com", "temporary_password": "manager-password-123", "role": "manager"},
    )
    assert create_user.status_code == 201

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Team schedule",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    client.post("/auth/logout")
    login_res = login(client, "manager@example.com", "manager-password-123")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True
    assert change_password(client, "manager-password-123", "manager-password-456").status_code == 200

    listing = client.get("/api/schedules")
    assert listing.status_code == 200
    assert any(item["id"] == schedule_id for item in listing.json())

    fetched = client.get(f"/api/schedules/{schedule_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == schedule_id


def test_manager_can_create_and_delete_saved_schedules():
    client = TestClient(app)
    bootstrap_admin(client)
    created = client.post(
        "/api/admin/users",
        json={"email": "manager@example.com", "temporary_password": "manager-password-123", "role": "manager"},
    )
    assert created.status_code == 201

    client.post("/auth/logout")
    login_res = login(client, "manager@example.com", "manager-password-123")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True
    assert change_password(client, "manager-password-123", "manager-password-456").status_code == 200

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Manager-owned schedule",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 200


def test_view_only_can_only_access_latest_two_saved_schedules():
    client = TestClient(app)
    bootstrap_admin(client)
    create_user = client.post(
        "/api/admin/users",
        json={"email": "viewer@example.com", "temporary_password": "viewer-password-123", "role": "view_only"},
    )
    assert create_user.status_code == 201

    created_ids: list[int] = []
    for weeks_ahead in (7, 14, 21):
        payload = _sample_payload_dict()
        payload["period"]["start_date"] = (date.today() + timedelta(days=weeks_ahead)).isoformat()
        generated = client.post("/generate", json=payload)
        assert generated.status_code == 200
        saved = client.post(
            "/api/schedules",
            json={
                "label": f"Schedule {weeks_ahead}",
                "period_start": payload["period"]["start_date"],
                "weeks": payload["period"]["weeks"],
                "payload_json": payload,
                "result_json": generated.json(),
            },
        )
        assert saved.status_code == 201
        created_ids.append(saved.json()["id"])

    client.post("/auth/logout")
    login_res = login(client, "viewer@example.com", "viewer-password-123")
    assert login_res.status_code == 200
    assert login_res.json()["must_change_password"] is True
    assert change_password(client, "viewer-password-123", "viewer-password-456").status_code == 200

    limited_listing = client.get("/api/view-only/schedules")
    assert limited_listing.status_code == 200
    limited_items = limited_listing.json()
    assert len(limited_items) == 2
    assert [item["id"] for item in limited_items] == [created_ids[2], created_ids[1]]
    assert "payload_json" not in limited_items[0]
    assert isinstance(limited_items[0]["assignments"], list)

    assert client.get("/api/schedules").status_code == 403
    assert client.get(f"/api/schedules/{created_ids[2]}").status_code == 403
    assert client.get("/api/employees").status_code == 403
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=28)).isoformat()
    assert client.post("/generate", json=payload).status_code == 403


def test_two_browser_sessions_can_see_same_saved_schedule():
    chrome = TestClient(app)
    safari = TestClient(app)
    bootstrap_admin(chrome, "multi@example.com", "multi-password-123")

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = chrome.post("/generate", json=payload)
    assert generated.status_code == 200

    saved = chrome.post(
        "/api/schedules",
        json={
            "label": "Cross-browser",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    login_safari = safari.post("/auth/login", json={"email": "multi@example.com", "password": "multi-password-123"})
    assert login_safari.status_code == 200

    listing = safari.get("/api/schedules")
    assert listing.status_code == 200
    assert any(item["id"] == schedule_id for item in listing.json())

    loaded = safari.get(f"/api/schedules/{schedule_id}")
    assert loaded.status_code == 200
    assert loaded.json()["label"] == "Cross-browser"


def test_admin_can_delete_individual_saved_schedule():
    client = TestClient(app)
    bootstrap_admin(client)

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200

    saved = client.post(
        "/api/schedules",
        json={
            "label": "Delete me",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    deleted = client.delete(f"/api/schedules/{schedule_id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    listing = client.get("/api/schedules")
    assert listing.status_code == 200
    assert all(item["id"] != schedule_id for item in listing.json())
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404


def test_admin_can_delete_all_saved_schedules():
    client = TestClient(app)
    bootstrap_admin(client)

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200

    first = client.post(
        "/api/schedules",
        json={
            "label": "Delete all A",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert first.status_code == 201

    payload_b = _sample_payload_dict()
    payload_b["period"]["start_date"] = (date.today() + timedelta(days=14)).isoformat()
    generated_b = client.post("/generate", json=payload_b)
    assert generated_b.status_code == 200
    second = client.post(
        "/api/schedules",
        json={
            "label": "Delete all B",
            "period_start": payload_b["period"]["start_date"],
            "weeks": payload_b["period"]["weeks"],
            "payload_json": payload_b,
            "result_json": generated_b.json(),
        },
    )
    assert second.status_code == 201

    deleted = client.delete("/api/schedules")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert deleted.json()["deleted"] >= 2

    listing = client.get("/api/schedules")
    assert listing.status_code == 200
    assert listing.json() == []


def test_view_only_cannot_delete_saved_schedules():
    client = TestClient(app)
    bootstrap_admin(client)
    create_user = client.post(
        "/api/admin/users",
        json={"email": "viewer@example.com", "temporary_password": "viewer-password-123", "role": "view_only"},
    )
    assert create_user.status_code == 201

    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    generated = client.post("/generate", json=payload)
    assert generated.status_code == 200
    saved = client.post(
        "/api/schedules",
        json={
            "label": "Admin-owned schedule",
            "period_start": payload["period"]["start_date"],
            "weeks": payload["period"]["weeks"],
            "payload_json": payload,
            "result_json": generated.json(),
        },
    )
    assert saved.status_code == 201
    schedule_id = saved.json()["id"]

    client.post("/auth/logout")
    assert login(client, "viewer@example.com", "viewer-password-123").status_code == 200

    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 403
    assert client.delete("/api/schedules").status_code == 403


def test_generate_requires_authentication():
    client = TestClient(app)
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    unauthorized = client.post("/generate", json=payload)
    assert unauthorized.status_code == 401
