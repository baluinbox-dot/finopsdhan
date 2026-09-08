"""Tests for parameter sweeps in the in-app Backtest feature: submitting
several values for one param queues one backtest_runs row per value
(sharing a sweep_id), only the first is dispatched immediately, and
app.engine.scheduler's periodic advance_backtest_queue() is what makes the
rest ever actually run -- see app/routers/backtest.py's module docstring
for the full design. subprocess.Popen is always mocked here -- these
tests must never actually spawn scripts/backtest/run_single_backtest.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.models import BacktestRun, Strategy, User
from app.routers.backtest import (
    _active_lock_row,
    _next_undispatched_run,
    _reap_stale_runs,
    _sweepable_params,
    advance_backtest_queue,
)

CAPTCHA_ANSWER = "8"  # conftest.client patches random.randint to always return 4


def _register_and_login(client, db_session, email: str, password: str = "supersecret1"):
    client.get("/auth/logout")
    client.get("/auth/register")
    client.post(
        "/auth/register",
        data={"email": email, "password": password, "confirm_password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    user = db_session.scalar(select(User).where(User.email == email))
    user.email_verified = True
    user.is_approved = True
    db_session.commit()

    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    return user


def _publish_strategy(client, db_session, code_ref: str = "dynamic_strangle") -> Strategy:
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env
    client.post(
        "/strategies/admin/create",
        data={"name": "Dynamic Strangle", "description": "test", "code_ref": code_ref, "default_params_json": "{}"},
        follow_redirects=False,
    )
    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == code_ref))
    client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    db_session.refresh(strategy)
    return strategy


def _mock_popen():
    proc = MagicMock()
    proc.pid = 999
    return patch("app.routers.backtest.subprocess.Popen", return_value=proc)


# --- _sweepable_params ---


def test_sweepable_params_keeps_only_top_level_numeric_non_bool_fields():
    params = {
        "underlying": "NIFTY", "lots": 2, "base_distance_points": 500.0, "daily_stop_loss": 10000,
        "hedge_enabled": True, "order_type": "LIMIT", "nested": {"a": 1},
    }
    assert _sweepable_params(params) == ["base_distance_points", "daily_stop_loss"]


# --- dispatched vs undispatched distinction ---


def test_active_lock_row_ignores_an_undispatched_queued_row(db_session):
    user = User(email="a@example.com", password_hash="x")
    strategy = Strategy(name="s", code_ref="dynamic_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()

    db_session.add(BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=None,
    ))
    db_session.commit()

    assert _active_lock_row(db_session) is None


def test_active_lock_row_counts_a_dispatched_queued_row(db_session):
    user = User(email="a@example.com", password_hash="x")
    strategy = Strategy(name="s", code_ref="dynamic_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()

    db_session.add(BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=1234,
    ))
    db_session.commit()

    assert _active_lock_row(db_session) is not None


def test_reap_stale_runs_never_touches_an_undispatched_queued_row(db_session):
    """An undispatched sweep member is a real wait queue, not a stuck
    state -- it must survive the stale-reap sweep no matter how old it is."""
    user = User(email="a@example.com", password_hash="x")
    strategy = Strategy(name="s", code_ref="dynamic_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()

    old_undispatched = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(old_undispatched)
    db_session.commit()

    _reap_stale_runs(db_session)

    db_session.refresh(old_undispatched)
    assert old_undispatched.status == "queued"  # untouched


# --- advance_backtest_queue ---


def test_advance_backtest_queue_dispatches_the_oldest_undispatched_row(db_session):
    user = User(email="a@example.com", password_hash="x")
    strategy = Strategy(name="s", code_ref="dynamic_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()

    older = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=None,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    newer = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=None,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    with _mock_popen() as mock_popen:
        advance_backtest_queue(db_session)

    mock_popen.assert_called_once()
    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.pid == 999
    assert newer.pid is None  # not this tick's turn


def test_advance_backtest_queue_does_nothing_while_the_lock_is_held(db_session):
    user = User(email="a@example.com", password_hash="x")
    strategy = Strategy(name="s", code_ref="dynamic_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.commit()

    db_session.add(BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="running",
    ))
    waiting = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued", pid=None,
    )
    db_session.add(waiting)
    db_session.commit()

    with _mock_popen() as mock_popen:
        advance_backtest_queue(db_session)

    mock_popen.assert_not_called()
    db_session.refresh(waiting)
    assert waiting.status == "queued"
    assert waiting.pid is None


def test_advance_backtest_queue_is_a_noop_when_nothing_is_waiting(db_session):
    with _mock_popen() as mock_popen:
        advance_backtest_queue(db_session)
    mock_popen.assert_not_called()


# --- sweep submission (router-level) ---


def test_submit_with_a_sweep_creates_one_row_per_value_only_first_dispatched(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path), _mock_popen() as mock_popen:
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={
                "underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1,
                "sweep_param": "base_distance_points", "sweep_values": "300, 500, 1000",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_popen.assert_called_once()  # only the first value's subprocess spawns inline

    runs = db_session.scalars(
        select(BacktestRun).where(BacktestRun.strategy_id == strategy.id).order_by(BacktestRun.sweep_value.asc())
    ).all()
    assert len(runs) == 3
    sweep_id = runs[0].sweep_id
    assert sweep_id is not None
    assert all(r.sweep_id == sweep_id for r in runs)
    assert [float(r.sweep_value) for r in runs] == [300.0, 500.0, 1000.0]
    assert [r.params["base_distance_points"] for r in runs] == [300.0, 500.0, 1000.0]
    # only the first (lowest value, earliest created) got a subprocess
    dispatched = [r for r in runs if r.pid is not None]
    assert len(dispatched) == 1
    assert resp.headers["location"] == f"/backtest/{strategy.id}/sweeps/{sweep_id}"


def test_submit_sweep_rejects_a_non_sweepable_param(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path):
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={
                "underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1,
                "sweep_param": "underlying", "sweep_values": "NIFTY,BANKNIFTY",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert db_session.scalars(select(BacktestRun)).first() is None


@pytest.mark.parametrize("values,expected_count", [("300", 0), ("300,300,300", 0), ("a,b", 0)])
def test_submit_sweep_rejects_bad_values(client, db_session, tmp_path, values, expected_count):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path):
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={
                "underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1,
                "sweep_param": "base_distance_points", "sweep_values": values,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert db_session.scalars(select(BacktestRun)).all().__len__() == expected_count


def test_submit_sweep_rejects_more_than_the_max_values(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    too_many = ",".join(str(100 * i) for i in range(1, 12))  # 11 values, cap is 10
    with patch("app.routers.backtest.DATA_ROOT", tmp_path):
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={
                "underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1,
                "sweep_param": "base_distance_points", "sweep_values": too_many,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert db_session.scalars(select(BacktestRun)).first() is None


# --- sweep comparison page ---


def test_sweep_status_page_shows_headline_and_chart_once_all_done(client, db_session):
    strategy = _publish_strategy(client, db_session)
    user = _register_and_login(client, db_session, "trader@example.com")

    import uuid as uuid_mod
    sweep_id = uuid_mod.uuid4()
    runs = [
        BacktestRun(
            user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
            start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"base_distance_points": v},
            status="completed", sweep_id=sweep_id, sweep_param="base_distance_points", sweep_value=v,
            result={"trade_count": 10, "total_pnl": pnl, "win_rate": 60.0, "max_drawdown": 1000.0},
        )
        for v, pnl in [(300.0, -500.0), (500.0, 2000.0), (1000.0, 800.0)]
    ]
    db_session.add_all(runs)
    db_session.commit()

    resp = client.get(f"/backtest/{strategy.id}/sweeps/{sweep_id}")
    assert resp.status_code == 200
    assert "500.0" in resp.text  # the best (highest P&L) value shown
    assert "(best)" in resp.text
    assert "<meta http-equiv=\"refresh\"" not in resp.text  # all done -- no auto-refresh


def test_sweep_status_page_auto_refreshes_while_incomplete(client, db_session):
    strategy = _publish_strategy(client, db_session)
    user = _register_and_login(client, db_session, "trader@example.com")

    import uuid as uuid_mod
    sweep_id = uuid_mod.uuid4()
    db_session.add_all([
        BacktestRun(
            user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
            start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"base_distance_points": 300.0},
            status="completed", sweep_id=sweep_id, sweep_param="base_distance_points", sweep_value=300.0,
            result={"trade_count": 5, "total_pnl": 100.0, "win_rate": 50.0, "max_drawdown": 200.0},
        ),
        BacktestRun(
            user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
            start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"base_distance_points": 500.0},
            status="queued", pid=None, sweep_id=sweep_id, sweep_param="base_distance_points", sweep_value=500.0,
        ),
    ])
    db_session.commit()

    resp = client.get(f"/backtest/{strategy.id}/sweeps/{sweep_id}")
    assert resp.status_code == 200
    assert "<meta http-equiv=\"refresh\"" in resp.text


def test_sweep_status_page_hides_another_users_sweep(client, db_session):
    strategy = _publish_strategy(client, db_session)
    owner = _register_and_login(client, db_session, "first@example.com")

    import uuid as uuid_mod
    sweep_id = uuid_mod.uuid4()
    db_session.add(BacktestRun(
        user_id=owner.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"base_distance_points": 300.0},
        status="queued", pid=None, sweep_id=sweep_id, sweep_param="base_distance_points", sweep_value=300.0,
    ))
    db_session.commit()

    _register_and_login(client, db_session, "second@example.com")
    resp = client.get(f"/backtest/{strategy.id}/sweeps/{sweep_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/backtest/{strategy.id}"
