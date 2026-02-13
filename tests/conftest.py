from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as app_db

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("BOOTSTRAP_TOKEN", "test-bootstrap-token")

@pytest.fixture(autouse=True)
def reset_database(tmp_path, monkeypatch):
    db_file = tmp_path / "test_auth_suite.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    # Rebuild DB bindings per test so every test gets its own writable SQLite file.
    app_db.engine.dispose()
    app_db.DATABASE_URL = app_db.get_database_url()
    app_db.engine = create_engine(
        app_db.DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    app_db.SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=app_db.engine,
        expire_on_commit=False,
    )

    app_db.Base.metadata.drop_all(bind=app_db.engine)
    app_db.Base.metadata.create_all(bind=app_db.engine)
    yield
    app_db.Base.metadata.drop_all(bind=app_db.engine)
    app_db.engine.dispose()
