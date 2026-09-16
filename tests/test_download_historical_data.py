"""Tests for scripts/backtest/download_historical_data.py's optional
DataDownloadRun status reporting (main(run_id=...)) -- added so the
admin-triggered "Data Download" page (app.routers.admin) can reflect
success/failure. The actual download logic (download_spot/
download_options) is mocked here; it has no dedicated test coverage of
its own since it only ever talks to the real Dhan API."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "backtest"))

from app.db import Base  # noqa: E402
from app.models import DataDownloadRun, User, UserRole  # noqa: E402
from download_historical_data import main  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _seed_run(db_session) -> DataDownloadRun:
    user = User(email="baluinbox@gmail.com", password_hash="x", role=UserRole.SUPERADMIN)
    db_session.add(user)
    db_session.commit()
    run = DataDownloadRun(started_by_user_id=user.id, status="running")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def test_main_with_no_run_id_never_touches_the_db(monkeypatch):
    """Plain manual `python download_historical_data.py` (no argument) --
    must behave exactly as it always has, no DB session opened at all."""
    monkeypatch.setattr("download_historical_data.SessionLocal", MagicMock(side_effect=AssertionError("must not be called")))
    with (
        patch("download_historical_data._get_client", return_value=MagicMock()),
        patch("download_historical_data.download_spot"),
        patch("download_historical_data.download_options"),
    ):
        main(None)  # must not raise


def test_main_marks_the_run_completed_on_success(db_session, monkeypatch):
    run = _seed_run(db_session)
    monkeypatch.setattr("download_historical_data.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)  # don't tear down the fixture's own session

    with (
        patch("download_historical_data._get_client", return_value=MagicMock()),
        patch("download_historical_data.download_spot") as mock_spot,
        patch("download_historical_data.download_options") as mock_options,
    ):
        main(str(run.id))

    assert mock_spot.call_count == 3  # NIFTY, BANKNIFTY, SENSEX
    assert mock_options.call_count == 3
    db_session.refresh(run)
    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.error_message == ""


def test_main_marks_the_run_failed_and_reraises_on_exception(db_session, monkeypatch):
    run = _seed_run(db_session)
    monkeypatch.setattr("download_historical_data.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    with (
        patch("download_historical_data._get_client", side_effect=RuntimeError("baluinbox@gmail.com not found in this DB")),
        pytest.raises(RuntimeError),
    ):
        main(str(run.id))

    db_session.refresh(run)
    assert run.status == "failed"
    assert "not found in this DB" in run.error_message
    assert run.finished_at is not None
