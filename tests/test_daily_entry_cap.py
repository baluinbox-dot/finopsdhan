"""A manual "Close Now" shouldn't burn a strategy's one-entry-per-day
slot — only its own automatic exits (stop-loss/target/window-end/per-leg
rule) should. Covers app.engine.runner._today_run_count and the
manually_closed flag it reads, set by _close_open_run/close_user_strategy_now."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine.runner import _close_open_run, _today_run_count, close_user_strategy_now
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy


def _make_user_strategy(db_session) -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True)
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def _add_run(db_session, user_strategy: UserStrategy, *, status: str, manually_closed: bool) -> StrategyRun:
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status=status,
        manually_closed=manually_closed,
        legs_planned={"legs": [], "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)
    return run


def test_manually_closed_run_excluded_from_daily_count(db_session):
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=True)
    assert _today_run_count(user_strategy) == 0


def test_automatically_closed_run_counts_toward_daily_cap(db_session):
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=False)
    assert _today_run_count(user_strategy) == 1


def test_mixed_runs_only_automatic_ones_count(db_session):
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=True)
    _add_run(db_session, user_strategy, status="closed", manually_closed=False)
    _add_run(db_session, user_strategy, status="closed", manually_closed=True)
    assert _today_run_count(user_strategy) == 1


def test_close_user_strategy_now_marks_run_manually_closed(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={
            "legs": [{
                "label": "SELL X", "security_id": "1", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
                "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT", "product_type": "INTRADAY",
                "price": 50.0, "role": "primary",
            }],
            "entry_premium": 50.0,
        },
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 45.0}}}}}
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    closed = close_user_strategy_now(db_session, user_strategy)
    assert closed is True

    db_session.refresh(run)
    assert run.status == "closed"
    assert run.manually_closed is True
    assert _today_run_count(user_strategy) == 0  # doesn't burn today's slot


def test_scheduled_close_open_run_does_not_mark_manual(db_session):
    user_strategy = _make_user_strategy(db_session)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={
            "legs": [{
                "label": "SELL X", "security_id": "1", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
                "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT", "product_type": "INTRADAY",
                "price": 50.0, "role": "primary",
            }],
            "entry_premium": 50.0,
        },
    )
    db_session.add(run)
    db_session.commit()

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 20.0}}}}}

    _close_open_run(db_session, dhan, user_strategy.user_id, run, is_live=False, reason="Exit conditions met.")

    db_session.refresh(run)
    db_session.refresh(user_strategy)
    assert run.manually_closed is False
    assert _today_run_count(user_strategy) == 1  # a real automatic exit still burns today's slot
