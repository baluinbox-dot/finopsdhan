"""Realized P&L is computed and saved the moment a position closes —
nothing was persisted before this. Covers app.strategies.base.leg_pnl and
its wiring into _close_open_run (both the scheduled-exit and manual
Close Now paths, which share it)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine.runner import _close_open_run, close_user_strategy_now
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import leg_pnl


def _sell_leg(price: float, quantity: int = 75) -> dict:
    return {
        "label": "SELL X", "security_id": "1", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "primary",
    }


def _buy_leg(price: float, quantity: int = 75) -> dict:
    return {
        "label": "BUY X", "security_id": "2", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
        "transaction_type": "BUY", "quantity": quantity, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "hedge",
    }


def test_leg_pnl_sell_profits_when_bought_back_cheaper():
    assert leg_pnl(_sell_leg(50.0), 30.0) == (50.0 - 30.0) * 75


def test_leg_pnl_sell_loses_when_bought_back_dearer():
    assert leg_pnl(_sell_leg(50.0), 70.0) == (50.0 - 70.0) * 75


def test_leg_pnl_buy_profits_when_sold_dearer():
    assert leg_pnl(_buy_leg(10.0), 15.0) == (15.0 - 10.0) * 75


def test_leg_pnl_buy_loses_when_sold_cheaper():
    assert leg_pnl(_buy_leg(10.0), 4.0) == (4.0 - 10.0) * 75


def _make_open_run(db_session, *, sell_price=50.0, buy_price=None) -> StrategyRun:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True)
    db_session.add(user_strategy)
    db_session.flush()

    legs = [_sell_leg(sell_price)]
    if buy_price is not None:
        legs.append(_buy_leg(buy_price))

    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={"legs": legs, "entry_premium": sell_price},
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_close_open_run_saves_realized_pnl_and_closed_at(db_session):
    run = _make_open_run(db_session, sell_price=50.0)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 30.0}}}},
    }

    assert run.closed_at is None
    assert float(run.realized_pnl) == 0

    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="Exit conditions met.")

    db_session.refresh(run)
    assert run.status == "closed"
    assert run.closed_at is not None
    assert float(run.realized_pnl) == (50.0 - 30.0) * 75


def test_close_open_run_accumulates_pnl_across_multiple_legs(db_session):
    # SELL @50 bought back @30 (profit) + hedge BUY @10 sold @6 (loss).
    run = _make_open_run(db_session, sell_price=50.0, buy_price=10.0)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 30.0}, "2": {"last_price": 6.0}}}},
    }

    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="Exit conditions met.")

    db_session.refresh(run)
    expected = (50.0 - 30.0) * 75 + (6.0 - 10.0) * 75
    assert float(run.realized_pnl) == expected


def test_close_user_strategy_now_also_saves_realized_pnl(db_session, monkeypatch):
    run = _make_open_run(db_session, sell_price=50.0)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 20.0}}}},
    }
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    assert close_user_strategy_now(db_session, run.user_strategy) is True

    db_session.refresh(run)
    assert float(run.realized_pnl) == (50.0 - 20.0) * 75
    assert run.manually_closed is True
    assert run.closed_at is not None


def test_close_open_run_falls_back_to_entry_price_without_a_fresh_quote(db_session):
    """No quote for the leg -> exit priced at the stale entry price ->
    zero P&L for that leg, not a crash and not a guessed number."""
    run = _make_open_run(db_session, sell_price=50.0)
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {}}}

    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="Exit conditions met.")

    db_session.refresh(run)
    assert float(run.realized_pnl) == 0.0
