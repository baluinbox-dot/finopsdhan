from __future__ import annotations

import uuid

from app.engine.pnl import compute_max_profit_loss, strategy_payoff_extremes
from app.models import Strategy, StrategyMode, StrategyRun, UserStrategy


def _leg(strike: int, option_type: str, txn: str, price: float, quantity: int = 75, security_id: str = "1") -> dict:
    return {
        "security_id": security_id,
        "exchange_segment": "NSE_FNO",
        "transaction_type": txn,
        "quantity": quantity,
        "price": price,
        "trading_symbol": f"NIFTY {strike} {option_type} 2026-12-31",
    }


def _make_open_position(legs: list[dict], *, leg_state: dict | None = None, realized_pnl: float = 0.0) -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Test Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(
        id=uuid.uuid4(), user_id=uuid.uuid4(), strategy_id=strategy.id,
        params={}, mode=StrategyMode.PAPER, is_active=True,
    )
    us.strategy = strategy
    legs_planned = {"legs": legs, "entry_premium": 0}
    if leg_state is not None:
        legs_planned["leg_state"] = leg_state
    run = StrategyRun(
        id=uuid.uuid4(), user_strategy_id=us.id, status="open",
        legs_planned=legs_planned, realized_pnl=realized_pnl,
    )
    us.runs = [run]
    return us


def _make_flat_strategy() -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Flat Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(
        id=uuid.uuid4(), user_id=uuid.uuid4(), strategy_id=strategy.id,
        params={}, mode=StrategyMode.PAPER, is_active=True,
    )
    us.strategy = strategy
    us.runs = []
    return us


# --- strategy_payoff_extremes: the core payoff-curve math ---


def test_iron_fly_is_bounded_both_sides_matching_the_credit_and_wing_width():
    """CES 24000 sell @150, PES 24000 sell @140, CEB 24300 buy @50, PEB
    23700 buy @45, qty 75 -- an Iron Fly with equal 300-pt wings both sides.
    Net credit = (150+140-50-45)*75 = 14625 (== max profit, ATM at expiry).
    Max loss = 300*75 - 14625 = 7875 (either wing touched at expiry)."""
    legs = [
        _leg(24000, "CE", "SELL", 150.0, security_id="ces"),
        _leg(24000, "PE", "SELL", 140.0, security_id="pes"),
        _leg(24300, "CE", "BUY", 50.0, security_id="ceb"),
        _leg(23700, "PE", "BUY", 45.0, security_id="peb"),
    ]

    result = strategy_payoff_extremes(legs)

    assert result["max_profit"] == 14625.0
    assert result["max_loss"] == -7875.0


def test_naked_short_call_has_capped_profit_and_unlimited_loss():
    legs = [_leg(24000, "CE", "SELL", 150.0)]

    result = strategy_payoff_extremes(legs)

    assert result["max_profit"] == 150.0 * 75  # capped at the premium collected
    assert result["max_loss"] is None  # unbounded as spot rallies


def test_naked_short_put_has_capped_profit_and_unlimited_loss():
    legs = [_leg(24000, "PE", "SELL", 140.0)]

    result = strategy_payoff_extremes(legs)

    assert result["max_profit"] == 140.0 * 75
    assert result["max_loss"] is None  # unbounded as spot falls


def test_naked_long_put_has_capped_loss_and_unlimited_profit():
    legs = [_leg(24000, "PE", "BUY", 50.0)]

    result = strategy_payoff_extremes(legs)

    assert result["max_profit"] is None  # unbounded as spot falls
    assert result["max_loss"] == -50.0 * 75  # capped at the premium paid


def test_naked_long_call_has_capped_loss_and_unlimited_profit():
    legs = [_leg(24000, "CE", "BUY", 50.0)]

    result = strategy_payoff_extremes(legs)

    assert result["max_profit"] is None  # unbounded as spot rallies
    assert result["max_loss"] == -50.0 * 75


def test_hedged_single_leg_seller_is_bounded_on_the_sold_side():
    """SELL 24000 CE @150 + BUY 24300 CE (hedge) @50, qty 75 -- a bull call
    credit spread. Max profit = net credit; max loss = wing width - credit;
    unbounded left (nothing sold/bought below 24000)."""
    legs = [
        _leg(24000, "CE", "SELL", 150.0, security_id="sold"),
        _leg(24300, "CE", "BUY", 50.0, security_id="hedge"),
    ]

    result = strategy_payoff_extremes(legs)

    net_credit = (150.0 - 50.0) * 75
    assert result["max_profit"] == net_credit
    assert result["max_loss"] == net_credit - 300 * 75


def test_realized_pnl_so_far_is_added_as_a_flat_offset_to_both():
    legs = [
        _leg(24000, "CE", "SELL", 150.0, security_id="ces"),
        _leg(24000, "PE", "SELL", 140.0, security_id="pes"),
        _leg(24300, "CE", "BUY", 50.0, security_id="ceb"),
        _leg(23700, "PE", "BUY", 45.0, security_id="peb"),
    ]

    result = strategy_payoff_extremes(legs, realized_so_far=-2000.0)

    assert result["max_profit"] == 14625.0 - 2000.0
    assert result["max_loss"] == -7875.0 - 2000.0


def test_returns_none_for_both_when_a_leg_strike_cant_be_parsed():
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "price": 150.0, "trading_symbol": "garbage"}]

    result = strategy_payoff_extremes(legs)

    assert result == {"max_profit": None, "max_loss": None}


def test_empty_legs_returns_none_for_both():
    assert strategy_payoff_extremes([]) == {"max_profit": None, "max_loss": None}


# --- compute_max_profit_loss: per-UserStrategy wiring ---


def test_compute_skips_flat_strategies():
    us = _make_flat_strategy()
    assert compute_max_profit_loss([us]) == []


def test_compute_excludes_legs_already_closed_and_includes_realized_pnl():
    legs = [
        _leg(24150, "PE", "SELL", 131.60, security_id="24150"),  # closed by an earlier roll
        _leg(24000, "PE", "SELL", 77.20, security_id="24000"),  # currently open
    ]
    us = _make_open_position(legs, leg_state={"24150": {"status": "closed"}}, realized_pnl=-2500.0)

    results = compute_max_profit_loss([us])

    assert len(results) == 1
    r = results[0]
    assert r["user_strategy_id"] == str(us.id)
    # Only the open 24000 PE sell is priced into the curve -- naked, so
    # unlimited loss; max profit = its own premium (77.20*65... here qty 75) plus realized.
    assert r["max_loss"] is None
    assert r["max_profit"] == 77.20 * 75 - 2500.0


def test_compute_returns_nothing_once_every_leg_has_closed():
    legs = [_leg(24150, "PE", "SELL", 131.60, security_id="24150")]
    us = _make_open_position(legs, leg_state={"24150": {"status": "closed"}})

    assert compute_max_profit_loss([us]) == []
