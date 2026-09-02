from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from app.engine.pnl import compute_live_pnl
from app.models import Strategy, StrategyMode, StrategyRun, UserStrategy


def _make_open_position(legs: list[dict], entry_premium: float, *, leg_state: dict | None = None, realized_pnl: float = 0.0) -> UserStrategy:
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
    legs_planned = {"legs": legs, "entry_premium": entry_premium}
    if leg_state is not None:
        legs_planned["leg_state"] = leg_state
    run = StrategyRun(
        id=uuid.uuid4(),
        user_strategy_id=us.id,
        status="open",
        legs_planned=legs_planned,
        realized_pnl=realized_pnl,
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
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 90.0}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 60.0}}}}}

    results = compute_live_pnl(dhan, [us])

    assert len(results) == 1
    r = results[0]
    assert r["priced"] is True
    assert r["pnl_total"] == (90.0 - 60.0) * 65
    assert r["legs"] == [{"security_id": "91", "current_price": 60.0}]


def test_pnl_loss_for_naked_seller():
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 90.0}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 130.0}}}}}

    results = compute_live_pnl(dhan, [us])
    r = results[0]
    assert r["pnl_total"] == (90.0 - 130.0) * 65
    assert r["pnl_total"] < 0


def test_pnl_accounts_for_hedge_leg():
    legs = [
        {"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 8.0},
        {"security_id": "101", "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 65, "price": 3.0},
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
    # SELL 8 -> mark 6 (profit 2/unit) + BUY 3 -> mark 2 (loss 1/unit) = 1/unit net -> *65
    assert r["pnl_total"] == 1.0 * 65


def test_pnl_batches_single_quote_call_across_positions():
    legs_a = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 8.0}]
    legs_b = [{"security_id": "41", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 90.0}]
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
    legs = [{"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 90.0}]
    us = _make_open_position(legs, entry_premium=90.0)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "failure", "remarks": "DH-904"}

    results = compute_live_pnl(dhan, [us])
    r = results[0]
    assert r["priced"] is False
    assert r["pnl_total"] is None
    assert r["legs"] == [{"security_id": "91", "current_price": None}]


def test_pnl_excludes_legs_already_closed_by_a_roll_and_adds_realized_pnl():
    """The bug this whole function was rewritten for: after a roll, a
    still-open run's `legs` list keeps the full history (old strikes +
    new ones — see app.engine.runner._apply_rolls). Only the currently-
    open leg should be priced/quoted; the closed one's fixed history is
    already reflected in run.realized_pnl, not re-priced against a live
    quote."""
    legs = [
        # Closed via a roll earlier today — must be excluded from pricing.
        {"security_id": "24150", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 131.60},
        # The pair the roll opened — still open right now.
        {"security_id": "24000", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 77.20},
    ]
    us = _make_open_position(
        legs, entry_premium=131.60,
        leg_state={"24150": {"status": "closed"}},
        realized_pnl=-2500.0,  # what the roll already booked closing the 24150 leg
    )

    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"24000": {"last_price": 70.0}}}},
    }

    results = compute_live_pnl(dhan, [us])

    assert len(results) == 1
    r = results[0]
    # Only the open leg was quoted for — 24150 never appears in the
    # request at all, not fetched-but-unused.
    requested_segment = dhan.quote_data.call_args[0][0]["NSE_FNO"]
    assert requested_segment == [24000]
    assert r["legs"] == [{"security_id": "24000", "current_price": 70.0}]
    unrealized = (77.20 - 70.0) * 65
    assert r["pnl_total"] == -2500.0 + unrealized


def test_pnl_does_not_double_count_a_security_id_revisited_after_an_earlier_close():
    """Regression for the bug found live 2026-09-02 (see
    tests/test_engine_rolls.py's engine-level version for the full
    writeup): a strike closed by an earlier roll and later reopened by a
    subsequent roll shares one security_id across two history entries.
    leg_state only tracks that sid's latest status, so before the
    currently_open_legs dedup fix, both entries priced into pnl_total,
    doubling this leg's contribution."""
    legs = [
        {"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 100.0},  # stale, closed
        {"security_id": "91", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 90.0},  # reopened, genuinely open
    ]
    us = _make_open_position(legs, entry_premium=90.0, leg_state={"91": {"status": "open"}})

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"91": {"last_price": 60.0}}}}}

    results = compute_live_pnl(dhan, [us])

    assert len(results) == 1
    r = results[0]
    # Only the reopened (90.0 entry) occurrence is priced -- once, not twice.
    assert r["legs"] == [{"security_id": "91", "current_price": 60.0}]
    assert r["pnl_total"] == (90.0 - 60.0) * 65  # not doubled


def test_pnl_returns_nothing_once_every_leg_has_closed_via_rolls():
    """A run can still be technically 'open' for a moment after its last
    leg closes (whole-position close hasn't run yet) — must not error or
    report a phantom position with zero legs."""
    legs = [{"security_id": "24150", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "price": 131.60}]
    us = _make_open_position(legs, entry_premium=131.60, leg_state={"24150": {"status": "closed"}})

    dhan = MagicMock()
    results = compute_live_pnl(dhan, [us])

    assert results == []
    dhan.quote_data.assert_not_called()
