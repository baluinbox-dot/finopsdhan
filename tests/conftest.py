from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Config must be set before app.config.get_settings() is first called anywhere.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key")
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", "12bGveMYeVKIeUw4EbOKDvN2Byo_WR80gNGObPw8YpQ=")
os.environ.setdefault("SUPERADMIN_EMAIL", "baluinbox@gmail.com")
os.environ.setdefault("ALLOW_LIVE_TRADING", "false")

from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402

get_settings.cache_clear()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session, monkeypatch):
    # The app's lifespan seeds a demo strategy and starts the background
    # scheduler against app.db.SessionLocal (the real configured DB), not the
    # per-test SQLite session above — irrelevant noise for these unit/route
    # tests, so no-op both during tests.
    monkeypatch.setattr("app.main.seed_demo_strategy", lambda: None)
    monkeypatch.setattr("app.main.start_scheduler", lambda: None)
    monkeypatch.setattr("app.main.stop_scheduler", lambda: None)

    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
