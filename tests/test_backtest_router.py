"""Router tests for the in-app Backtest feature (Phase C): form rendering,
submission safeguards (global one-at-a-time lock, per-user cooldown, stale-
run reaping), and the status page's three terminal states. subprocess.Popen
is always mocked here -- these tests must never actually spawn
scripts/backtest/run_single_backtest.py against the test DB."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.models import BacktestRun, Strategy, User

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


def _publish_strategy(client, db_session, code_ref: str = "rsi_call_writing") -> Strategy:
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env
    client.post(
        "/strategies/admin/create",
        data={"name": "RSI Call Writing — Weekly Roll", "description": "test", "code_ref": code_ref, "default_params_json": "{}"},
        follow_redirects=False,
    )
    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == code_ref))
    client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    db_session.refresh(strategy)
    return strategy


def _mock_popen():
    proc = MagicMock()
    proc.pid = 12345
    return patch("app.routers.backtest.subprocess.Popen", return_value=proc)


def test_backtest_form_rejects_a_strategy_not_backtest_ready(client, db_session):
    strategy = _publish_strategy(client, db_session, code_ref="atm_straddle_trigger_hedge")
    _register_and_login(client, db_session, "trader@example.com")

    resp = client.get(f"/backtest/{strategy.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"


def test_backtest_form_renders_for_a_ready_strategy(client, db_session):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")

    resp = client.get(f"/backtest/{strategy.id}")
    assert resp.status_code == 200
    assert "Run Backtest" in resp.text


def test_submit_rejects_an_underlying_with_no_local_data(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")

    with patch("app.routers.backtest.DATA_ROOT", tmp_path):  # empty -- no underlying subdirs at all
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/backtest/{strategy.id}"
    assert db_session.scalars(select(BacktestRun)).first() is None


def test_submit_creates_a_queued_run_and_spawns_a_subprocess(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path), _mock_popen() as mock_popen:
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 2},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_popen.assert_called_once()

    run = db_session.scalars(select(BacktestRun)).first()
    assert run is not None
    assert run.status == "queued"
    assert run.underlying == "NIFTY"
    assert run.params["lots"] == 2
    assert run.pid == 12345
    assert resp.headers["location"] == f"/backtest/{strategy.id}/runs/{run.id}"


def test_submit_rejects_start_after_end(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path):
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-10-04", "end_date": "2024-09-04", "lots": 1},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert db_session.scalars(select(BacktestRun)).first() is None


def test_submit_blocked_by_the_global_one_at_a_time_lock(client, db_session, tmp_path):
    """A second user's submission must be rejected while ANY backtest_runs
    row (regardless of whose) is queued/running -- the memory risk this
    guards against is shared VM-wide, not scoped to one user."""
    strategy = _publish_strategy(client, db_session)
    owner = _register_and_login(client, db_session, "first@example.com")
    (tmp_path / "NIFTY").mkdir()

    db_session.add(BacktestRun(
        user_id=owner.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="running",
    ))
    db_session.commit()

    _register_and_login(client, db_session, "second@example.com")
    with patch("app.routers.backtest.DATA_ROOT", tmp_path), _mock_popen() as mock_popen:
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_popen.assert_not_called()
    assert db_session.scalars(select(BacktestRun)).all().__len__() == 1  # only the pre-existing row


def test_submit_blocked_by_the_per_user_cooldown(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    user = _register_and_login(client, db_session, "trader@example.com")
    (tmp_path / "NIFTY").mkdir()

    # A completed run from moments ago -- doesn't hold the global lock, but
    # is recent enough to still be inside this user's own cooldown window.
    db_session.add(BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    db_session.commit()

    with patch("app.routers.backtest.DATA_ROOT", tmp_path), _mock_popen() as mock_popen:
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_popen.assert_not_called()


def test_stale_running_row_is_reaped_and_no_longer_blocks_submission(client, db_session, tmp_path):
    """A row stuck in 'running' well past the stale threshold (its
    subprocess likely got SIGKILLed by the OS OOM-killer, bypassing this
    app's own try/except) must not hold the global lock forever."""
    strategy = _publish_strategy(client, db_session)
    owner = _register_and_login(client, db_session, "first@example.com")
    (tmp_path / "NIFTY").mkdir()

    stale = BacktestRun(
        user_id=owner.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="running",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(stale)
    db_session.commit()

    _register_and_login(client, db_session, "second@example.com")
    with patch("app.routers.backtest.DATA_ROOT", tmp_path), _mock_popen() as mock_popen:
        resp = client.post(
            f"/backtest/{strategy.id}",
            data={"underlying": "NIFTY", "start_date": "2024-09-04", "end_date": "2024-10-04", "lots": 1},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_popen.assert_called_once()  # the stale row no longer blocks this submission

    db_session.refresh(stale)
    assert stale.status == "failed"
    assert "stale" in stale.error_message.lower() or "crashed" in stale.error_message.lower()


def test_status_page_renders_for_a_completed_run(client, db_session, tmp_path):
    strategy = _publish_strategy(client, db_session)
    user = _register_and_login(client, db_session, "trader@example.com")

    run = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={"lots": 1}, status="completed",
        result={
            "trade_count": 2, "total_pnl": 1234.5, "win_rate": 50.0, "max_drawdown": 500.0,
            "stale_trade_count": 0, "equity_curve": [["2024-09-04T10:00:00+05:30", 0.0]],
            "trades": [{
                "opened_at": "2024-09-04T09:20:00+05:30", "closed_at": "2024-09-04T14:45:00+05:30",
                "reason": "evaluate_exit", "realized_pnl": 1234.5, "costs": 40.0,
                "legs_opened": 2, "used_stale_price": False,
            }],
        },
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    resp = client.get(f"/backtest/{strategy.id}/runs/{run.id}")
    assert resp.status_code == 200
    assert "1,234" in resp.text or "1235" in resp.text  # total P&L rendered somewhere
    assert "evaluate_exit" in resp.text


def test_status_page_renders_for_a_failed_run(client, db_session):
    strategy = _publish_strategy(client, db_session)
    user = _register_and_login(client, db_session, "trader@example.com")

    run = BacktestRun(
        user_id=user.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="failed",
        error_message="MemoryError: hit the RLIMIT_AS cap",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    resp = client.get(f"/backtest/{strategy.id}/runs/{run.id}")
    assert resp.status_code == 200
    assert "MemoryError" in resp.text


def test_status_page_hides_another_users_run(client, db_session):
    strategy = _publish_strategy(client, db_session)
    owner = _register_and_login(client, db_session, "first@example.com")

    run = BacktestRun(
        user_id=owner.id, strategy_id=strategy.id, underlying="NIFTY",
        start_date=date(2024, 9, 4), end_date=date(2024, 10, 4), params={}, status="queued",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    _register_and_login(client, db_session, "second@example.com")
    resp = client.get(f"/backtest/{strategy.id}/runs/{run.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/backtest/{strategy.id}"
