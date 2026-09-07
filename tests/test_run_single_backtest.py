"""Tests for scripts/backtest/run_single_backtest.py -- the subprocess
entrypoint Phase C's in-app Backtest feature spawns per run. Covers the
equity-curve downsampling helper directly, and the end-to-end DB
read -> run_backtest -> DB write flow with run_backtest itself mocked (the
engine has its own dedicated test coverage in tests/test_backtest_engine.py)."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "backtest"))

from app.backtest.engine import BacktestResult, Trade  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import BacktestRun, Strategy, User, UserRole  # noqa: E402
from run_single_backtest import _downsample_equity_curve, main  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def test_downsample_equity_curve_keeps_the_last_tick_of_each_day():
    curve = [
        (datetime(2024, 9, 4, 9, 20, tzinfo=IST), 0.0),
        (datetime(2024, 9, 4, 14, 45, tzinfo=IST), 100.0),  # same day, later -> wins
        (datetime(2024, 9, 5, 9, 20, tzinfo=IST), 50.0),
    ]
    out = _downsample_equity_curve(curve)
    assert out == [
        ["2024-09-04T14:45:00+05:30", 100.0],
        ["2024-09-05T09:20:00+05:30", 50.0],
    ]


def test_downsample_equity_curve_empty():
    assert _downsample_equity_curve([]) == []


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


def _seed_run(db_session) -> BacktestRun:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="RSI Call Writing", code_ref="rsi_call_writing", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()
    run = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"lots": 1}, status="queued",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def test_main_writes_a_completed_result_on_success(db_session, monkeypatch):
    run = _seed_run(db_session)
    monkeypatch.setattr("run_single_backtest.SessionLocal", lambda: db_session)
    monkeypatch.setattr("run_single_backtest._apply_memory_limit", lambda: None)

    fake_result = BacktestResult(underlying="NIFTY")
    fake_result.trades.append(Trade(
        opened_at=datetime(2024, 9, 4, 9, 20, tzinfo=IST), closed_at=datetime(2024, 9, 4, 14, 45, tzinfo=IST),
        reason="evaluate_exit", realized_pnl=-40.0, costs=40.0, legs_opened=2,
    ))
    fake_result.equity_curve.append((datetime(2024, 9, 4, 14, 45, tzinfo=IST), -40.0))

    # db_session.close() is a real no-op-safe call the fixture also calls in
    # teardown -- patched out here so main()'s own `finally: db.close()`
    # doesn't tear down the fixture's session out from under the assertions below.
    monkeypatch.setattr(db_session, "close", lambda: None)

    with patch("run_single_backtest.run_backtest", return_value=fake_result) as mock_run_backtest:
        main(str(run.id))

    mock_run_backtest.assert_called_once()
    _, kwargs = mock_run_backtest.call_args
    assert kwargs["underlying"] == "NIFTY"
    assert kwargs["auto_advance_expiry"] is False  # rsi_call_writing isn't in AUTO_ADVANCE_EXPIRY_STRATEGIES

    db_session.refresh(run)
    assert run.status == "completed"
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.result["trade_count"] == 1
    assert run.result["total_pnl"] == -40.0
    assert run.result["trades"][0]["reason"] == "evaluate_exit"
    assert run.result["equity_curve"] == [["2024-09-04T14:45:00+05:30", -40.0]]


def test_main_writes_a_failed_result_on_exception(db_session, monkeypatch):
    run = _seed_run(db_session)
    monkeypatch.setattr("run_single_backtest.SessionLocal", lambda: db_session)
    monkeypatch.setattr("run_single_backtest._apply_memory_limit", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)

    with patch("run_single_backtest.run_backtest", side_effect=ValueError("No NIFTY data in range")):
        main(str(run.id))

    db_session.refresh(run)
    assert run.status == "failed"
    assert "No NIFTY data in range" in run.error_message
    assert run.finished_at is not None


def test_main_sets_auto_advance_expiry_for_iron_fly(db_session, monkeypatch):
    user = User(email="trader2@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Iron Fly with Adjustments", code_ref="iron_fly_adjustments", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()
    run = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"lots": 1}, status="queued",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    monkeypatch.setattr("run_single_backtest.SessionLocal", lambda: db_session)
    monkeypatch.setattr("run_single_backtest._apply_memory_limit", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)

    with patch("run_single_backtest.run_backtest", return_value=BacktestResult(underlying="NIFTY")) as mock_run_backtest:
        main(str(run.id))

    assert mock_run_backtest.call_args.kwargs["auto_advance_expiry"] is True
