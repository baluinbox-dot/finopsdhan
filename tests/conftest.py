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


@pytest.fixture(autouse=True)
def _no_option_chain_throttle(monkeypatch):
    # app.dhan.helpers throttles real option_chain calls to 1 per 3s
    # (Dhan's actual rate limit), per Dhan account (state keyed by
    # _client_key — see app/dhan/helpers.py). Tests mock the Dhan client,
    # so there's no real limit to respect — patch the interval itself to 0
    # rather than the throttle function, so the real per-account
    # throttle/backoff logic still runs (never sleeps at interval 0) instead
    # of being bypassed outright.
    monkeypatch.setattr("app.dhan.helpers._OPTION_CHAIN_MIN_INTERVAL_SECONDS", 0)


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

    # Login/register render a random "a + b" arithmetic CAPTCHA. Fix both
    # random draws to the same value so every test knows the expected
    # answer (CAPTCHA_ANSWER below) without needing to scrape it out of
    # rendered HTML.
    monkeypatch.setattr("app.routers.auth.random.randint", lambda lo, hi: 4)

    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# Matches the fixed random.randint patch above (4 + 4).
CAPTCHA_ANSWER = "8"
