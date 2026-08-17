from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from app.engine.pnl import compute_live_pnl
from app.models import Strategy, StrategyMode, StrategyRun, UserStrategy


def _make_open_position(legs: list[dict], entry_premium: float) -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Test Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        strategy_id=strategy.id,
        params={},
        mode=StrategyMode.PAPER,
        is_active=True,
    )
    us.strategy = strategy
    run = StrategyRun(
        id=uuid.uuid4(),
        user_strategy_id=us.id,
        status="open",
        legs_planned={"legs": legs, "entry_premium": entry_premium},
    )
    us.runs = [run]
    return us


def _make_flat_strategy() -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Flat Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        strategy_id=strategy.id,
        params={},
        mode=StrategyMode.PAPER,
        is_active=True,
    )
    us.strategy = strategy
    us.runs = []
    return us


def test_pnl_profit_for_naked_seller():
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 60.0}}}}}

    results = compute_live_pnl(dhan, [us])

    assert len(results) == 1
    r = results[0]
    assert r["priced"] is True
    assert r["current_value"] == 60.0
    assert r["pnl_total"] == (90.0 - 60.0) * 65


def test_pnl_loss_for_naked_seller():
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 130.0}}}}}

    results = compute_live_pnl(dhan, [us])
    r = results[0]
    assert r["pnl_total"] == (90.0 - 130.0) * 65
    assert r["pnl_total"] < 0


def test_pnl_accounts_for_hedge_leg():
    legs = [
        {"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65},
        {"security_id": "101", "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 65},
    ]
    # entry_premium = sell(8) - buy(3) = 5.0 net credit
    us = _make_open_position(legs, entry_premium=5.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 6.0}, "101": {"last_price": 2.0}}}},
    }

    results = compute_live_pnl(dhan, [us])
    r = results[0]
    # current_value = sell_leg(6.0) - buy_leg(2.0) = 4.0; pnl = entry(5.0) - current(4.0) = 1.0/unit
    assert r["current_value"] == 4.0
    assert r["pnl_total"] == 1.0 * 65


def test_pnl_batches_single_quote_call_across_positions():
    legs_a = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65}]
    legs_b = [{"security_id": "41", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65}]
    us_a = _make_open_position(legs_a, entry_premium=8.0)
    us_b = _make_open_position(legs_b, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 5.0}, "41": {"last_price": 95.0}}}},
    }

    results = compute_live_pnl(dhan, [us_a, us_b])

    assert dhan.quote_data.call_count == 1  # one batched call, not one per position
    assert len(results) == 2


def test_pnl_skips_flat_strategies():
    us = _make_flat_strategy()
    dhan = MagicMock()

    results = compute_live_pnl(dhan, [us])

    assert results == []
    dhan.quote_data.assert_not_called()


def test_pnl_marks_unpriced_when_quote_fails():
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "failure", "remarks": "DH-904"}

    results = compute_live_pnl(dhan, [us])
    r = results[0]
    assert r["priced"] is False
    assert r["pnl_total"] is None
