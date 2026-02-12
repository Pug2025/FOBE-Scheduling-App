from app.db import Base, engine
from fastapi.testclient import TestClient

from app.main import app


def test_smoke_dashboard_and_settings_update():
    Base.metadata.create_all(bind=engine)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    s = client.post(
        "/settings",
        data={"season": "summer", "start_date": "2026-01-01", "horizon_days": 7, "manager_consecutive_days_off": 2},
        follow_redirects=False,
    )
    assert s.status_code == 303
