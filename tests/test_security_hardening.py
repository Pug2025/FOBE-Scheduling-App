from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

BOOTSTRAP_TOKEN = "test-bootstrap-token"


def bootstrap_admin(client: TestClient, email: str = "admin@example.com", password: str = "admin-password-123"):
    return client.post(
        "/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": email, "password": password},
    )


def login(client: TestClient, email: str, password: str):
    return client.post("/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Security headers (H2, M1)
# ---------------------------------------------------------------------------

def test_security_headers_present_on_pages_and_api():
    client = TestClient(app)
    for path in ("/", "/health", "/auth/bootstrap/status"):
        res = client.get(path)
        assert res.headers.get("X-Frame-Options") == "SAMEORIGIN", path
        assert res.headers.get("X-Content-Type-Options") == "nosniff", path
        assert res.headers.get("Referrer-Policy") == "no-referrer", path
        assert "camera=()" in res.headers.get("Permissions-Policy", ""), path


def test_hsts_only_on_https():
    client = TestClient(app)
    plain = client.get("/health")
    assert "Strict-Transport-Security" not in plain.headers

    https = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    hsts = https.headers.get("Strict-Transport-Security", "")
    assert hsts.startswith("max-age=")
    assert "includeSubDomains" in hsts


def test_report_content_remains_embeddable_same_origin():
    """Guard against the clickjacking-header trap: the /reports page embeds the report
    content in a same-origin iframe, so the header must be SAMEORIGIN, never DENY."""
    client = TestClient(app)
    bootstrap_admin(client)
    html = "<!doctype html><html><body><h1>Embed check</h1></body></html>"
    upload = client.post("/api/reports/upload", files={"file": ("r.html", html, "text/html")})
    assert upload.status_code == 201
    content = client.get("/api/reports/current/content")
    assert content.status_code == 200
    assert content.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert content.headers.get("X-Content-Type-Options") == "nosniff"


def test_cache_control_still_no_store_on_api():
    client = TestClient(app)
    res = client.get("/auth/bootstrap/status")
    assert "no-store" in res.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# Generic login failures (M3)
# ---------------------------------------------------------------------------

def test_unknown_email_wrong_password_and_disabled_are_indistinguishable():
    admin = TestClient(app)
    bootstrap_admin(admin)
    created = admin.post(
        "/api/admin/users",
        json={"email": "staff@example.com", "temporary_password": "staff-password-123", "role": "manager"},
    )
    staff_id = created.json()["id"]
    admin.patch(f"/api/admin/users/{staff_id}", json={"is_active": False})

    anon = TestClient(app)
    unknown = login(anon, "ghost@example.com", "whatever-password-1")
    wrong = login(anon, "admin@example.com", "wrong-password-123")
    disabled = login(anon, "staff@example.com", "staff-password-123")

    assert unknown.status_code == wrong.status_code == disabled.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"] == disabled.json()["detail"]


def test_kiosk_unlock_disabled_account_gets_generic_401():
    admin = TestClient(app)
    bootstrap_admin(admin)
    created = admin.post(
        "/api/admin/users",
        json={"email": "mgr@example.com", "temporary_password": "mgr-password-1234", "role": "manager"},
    )
    mgr_id = created.json()["id"]
    admin.patch(f"/api/admin/users/{mgr_id}", json={"is_active": False})

    kiosk = TestClient(app)
    res = kiosk.post("/api/kiosk/unlock", json={"email": "mgr@example.com", "password": "mgr-password-1234"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_successful_login_unaffected():
    client = TestClient(app)
    bootstrap_admin(client)
    client.post("/auth/logout")
    assert login(client, "admin@example.com", "admin-password-123").status_code == 200


# ---------------------------------------------------------------------------
# Bootstrap token compare (L4)
# ---------------------------------------------------------------------------

def test_bootstrap_invalid_token_still_403():
    client = TestClient(app)
    res = client.post(
        "/auth/bootstrap",
        headers={"X-Bootstrap-Token": "wrong-token"},
        json={"email": "x@example.com", "password": "strong-password-123"},
    )
    assert res.status_code == 403


def test_bootstrap_missing_token_header_still_403():
    client = TestClient(app)
    res = client.post(
        "/auth/bootstrap",
        json={"email": "x@example.com", "password": "strong-password-123"},
    )
    assert res.status_code == 403
