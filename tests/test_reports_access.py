from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as app_main
from app.main import app

BOOTSTRAP_TOKEN = "test-bootstrap-token"

SAMPLE_REPORT_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Greystones Financial Explorer</title></head>"
    "<body><h1>Week 31</h1></body></html>"
)


def bootstrap_admin(client: TestClient, email: str = "admin@example.com", password: str = "admin-password-123"):
    return client.post(
        "/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": email, "password": password},
    )


def login(client: TestClient, email: str, password: str):
    return client.post("/auth/login", json={"email": email, "password": password})


def create_user(client: TestClient, email: str, role: str, password: str = "temp-password-123", report_access: bool | None = None):
    payload = {"email": email, "temporary_password": password, "role": role}
    if report_access is not None:
        payload["report_access"] = report_access
    return client.post("/api/admin/users", json=payload)


def change_password(client: TestClient, current_password: str, new_password: str):
    return client.post(
        "/auth/change-password",
        json={"current_password": current_password, "new_password": new_password},
    )


def make_active_user(admin_client_factory, email: str, role: str, report_access: bool | None = None):
    """Create a user, log in on a fresh client, and clear the forced password change."""
    admin = admin_client_factory()
    bootstrap_admin(admin)
    res = create_user(admin, email, role, report_access=report_access)
    assert res.status_code == 201, res.text
    user_id = res.json()["id"]

    user_client = TestClient(app)
    assert login(user_client, email, "temp-password-123").status_code == 200
    assert change_password(user_client, "temp-password-123", "new-password-456").status_code == 200
    return admin, user_client, user_id


# ---------------------------------------------------------------------------
# Access rules
# ---------------------------------------------------------------------------

def test_admin_has_report_access_by_default():
    client = TestClient(app)
    bootstrap_admin(client)
    me = client.get("/auth/me").json()
    assert me["report_access_effective"] is True


def test_manager_and_view_only_have_no_report_access_by_default():
    admin = TestClient(app)
    bootstrap_admin(admin)
    for role in ("manager", "view_only"):
        res = create_user(admin, f"{role}@example.com", role)
        assert res.status_code == 201, res.text
        assert res.json()["report_access_effective"] is False
        assert res.json()["report_access"] is False


def test_report_only_user_always_has_access():
    admin = TestClient(app)
    bootstrap_admin(admin)
    res = create_user(admin, "reportonly@example.com", "report_only")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["role"] == "report_only"
    assert body["report_access_effective"] is True


def test_admin_can_grant_report_access_to_manager_via_flag():
    admin = TestClient(app)
    bootstrap_admin(admin)
    created = create_user(admin, "mgr@example.com", "manager")
    user_id = created.json()["id"]
    assert created.json()["report_access_effective"] is False

    patched = admin.patch(f"/api/admin/users/{user_id}", json={"report_access": True})
    assert patched.status_code == 200
    assert patched.json()["report_access"] is True
    assert patched.json()["report_access_effective"] is True

    revoked = admin.patch(f"/api/admin/users/{user_id}", json={"report_access": False})
    assert revoked.status_code == 200
    assert revoked.json()["report_access_effective"] is False


# ---------------------------------------------------------------------------
# Reports endpoints gating
# ---------------------------------------------------------------------------

def test_reports_endpoints_require_authentication():
    client = TestClient(app)
    assert client.get("/api/reports/current").status_code == 401
    assert client.get("/api/reports/current/content").status_code == 401


def test_manager_without_access_is_blocked_from_reports():
    admin, manager_client, _ = make_active_user(lambda: TestClient(app), "mgr2@example.com", "manager")
    assert manager_client.get("/api/reports/current").status_code == 403
    # /reports page redirects users without access back to the root.
    page = manager_client.get("/reports", follow_redirects=False)
    assert page.status_code in (302, 303)
    assert page.headers["location"] == "/"


def test_report_only_user_can_view_but_is_confined_to_reports():
    admin, report_client, _ = make_active_user(lambda: TestClient(app), "ro@example.com", "report_only")

    # Can reach the reports API + page.
    assert report_client.get("/api/reports/current").status_code == 200
    assert report_client.get("/reports", follow_redirects=False).status_code == 200

    # Root redirects them to /reports; manager-only pages are off-limits.
    root = report_client.get("/", follow_redirects=False)
    assert root.status_code in (302, 303)
    assert root.headers["location"] == "/reports"
    assert report_client.get("/api/employees").status_code == 403
    assert report_client.get("/api/admin/users").status_code == 403


# ---------------------------------------------------------------------------
# Upload + serve
# ---------------------------------------------------------------------------

def test_admin_can_upload_and_report_only_user_can_read_it():
    admin = TestClient(app)
    bootstrap_admin(admin)
    create_user(admin, "ro2@example.com", "report_only")

    report_client = TestClient(app)
    assert login(report_client, "ro2@example.com", "temp-password-123").status_code == 200
    assert change_password(report_client, "temp-password-123", "new-password-456").status_code == 200

    # No report yet.
    assert report_client.get("/api/reports/current").json()["exists"] is False
    assert report_client.get("/api/reports/current/content").status_code == 404

    upload = admin.post(
        "/api/reports/upload",
        files={"file": ("report.html", SAMPLE_REPORT_HTML, "text/html")},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["exists"] is True
    assert upload.json()["source"] == "manual"

    meta = report_client.get("/api/reports/current")
    assert meta.status_code == 200
    assert meta.json()["exists"] is True

    content = report_client.get("/api/reports/current/content")
    assert content.status_code == 200
    assert "Week 31" in content.text


def test_non_admin_cannot_upload():
    admin, manager_client, _ = make_active_user(lambda: TestClient(app), "mgr3@example.com", "manager")
    res = manager_client.post(
        "/api/reports/upload",
        files={"file": ("report.html", SAMPLE_REPORT_HTML, "text/html")},
    )
    assert res.status_code == 403


def test_token_upload_works_when_configured(monkeypatch):
    monkeypatch.setattr(app_main, "REPORT_UPLOAD_TOKEN", "secret-token-123")
    anon = TestClient(app)  # no login at all

    bad = anon.post(
        "/api/reports/upload",
        files={"file": ("report.html", SAMPLE_REPORT_HTML, "text/html")},
        headers={"X-Report-Upload-Token": "wrong"},
    )
    assert bad.status_code == 403

    good = anon.post(
        "/api/reports/upload",
        files={"file": ("report.html", SAMPLE_REPORT_HTML, "text/html")},
        headers={"X-Report-Upload-Token": "secret-token-123"},
    )
    assert good.status_code == 201, good.text
    assert good.json()["source"] == "auto"


def test_upload_rejects_non_html_and_empty():
    admin = TestClient(app)
    bootstrap_admin(admin)

    empty = admin.post("/api/reports/upload", files={"file": ("x.html", "", "text/html")})
    assert empty.status_code == 400

    not_html = admin.post("/api/reports/upload", files={"file": ("x.txt", "just some text", "text/plain")})
    assert not_html.status_code == 400


def test_latest_upload_wins():
    admin = TestClient(app)
    bootstrap_admin(admin)
    admin.post("/api/reports/upload", files={"file": ("r1.html", "<html><body>OLD</body></html>", "text/html")})
    admin.post("/api/reports/upload", files={"file": ("r2.html", "<html><body>NEW</body></html>", "text/html")})
    content = admin.get("/api/reports/current/content")
    assert "NEW" in content.text
    assert "OLD" not in content.text
