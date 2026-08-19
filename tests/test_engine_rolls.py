"""Engine-level coverage for the roll machinery (_apply_rolls /
Strategy.evaluate_rolls) that app/strategies/three_pair_rolling.py depends
on — closing a group of legs and opening its replacement within one still-
open run, without touching any other leg."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine.runner import _apply_rolls, _close_open_run
from app.models import Order, Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg


def _ce_id(strike: int) -> str:
    return str(strike * 10 + 1)


def _pe_id(strike: int) -> str:
    return str(strike * 10 + 2)


def _pair_legs(pair_id: str, strike: int, price: float = 60.0) -> list[dict]:
    return [
        {"label": f"{pair_id} SELL {strike} CE", "security_id": _ce_id(strike), "trading_symbol": f"NIFTY {strike} CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": price, "role": "primary", "pair_id": pair_id},
        {"label": f"{pair_id} SELL {strike} PE", "security_id": _pe_id(strike), "trading_symbol": f"NIFTY {strike} PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": price, "role": "primary", "pair_id": pair_id},
    ]


def _roll_leg(pair_id: str, strike: int, price: float) -> list[OrderLeg]:
    return [
        OrderLeg(label=f"{pair_id} ROLL SELL {strike} CE", security_id=_ce_id(strike), trading_symbol=f"NIFTY {strike} CE 2026-08-27",
                 exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75, price=price, role="primary", pair_id=pair_id),
        OrderLeg(label=f"{pair_id} ROLL SELL {strike} PE", security_id=_pe_id(strike), trading_symbol=f"NIFTY {strike} PE 2026-08-27",
                 exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75, price=price, role="primary", pair_id=pair_id),
    ]


def _make_open_run(db_session) -> StrategyRun:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="3-Pair Rolling", code_ref="three_pair_rolling", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True)
    db_session.add(user_strategy)
    db_session.flush()

    legs = _pair_legs("FIN1", 24450) + _pair_legs("FIN2", 24400) + _pair_legs("FIN3", 24350)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={"legs": legs, "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_apply_rolls_closes_old_pair_and_opens_new_one(db_session):
    run = _make_open_run(db_session)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0}}}},
    }

    new_legs = _roll_leg("FIN1", 24300, 90.0)
    # Give CE/PE distinct prices to make the direction assertions unambiguous.
    new_legs[1].price = 15.0
    decision = {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": new_legs}]}

    _apply_rolls(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    assert run.status == "open"  # rolling never ends the run

    leg_state = run.legs_planned["leg_state"]
    assert leg_state[_ce_id(24450)]["status"] == "closed"
    assert leg_state[_pe_id(24450)]["status"] == "closed"
    assert leg_state[_ce_id(24300)]["status"] == "open"
    assert leg_state[_pe_id(24300)]["status"] == "open"
    # Other pairs completely untouched.
    assert _ce_id(24400) not in leg_state
    assert _ce_id(24350) not in leg_state

    # Old strike stays in leg history (needed for the unique-spot-per-day rule).
    all_security_ids = {leg["security_id"] for leg in run.legs_planned["legs"]}
    assert {
        _ce_id(24450), _pe_id(24450), _ce_id(24300), _pe_id(24300),
        _ce_id(24400), _pe_id(24400), _ce_id(24350), _pe_id(24350),
    } <= all_security_ids

    # P&L: SELL 60 -> bought back at 40 (profit) + SELL 60 -> bought back at 70 (loss).
    expected = (60.0 - 40.0) * 75 + (60.0 - 70.0) * 75
    assert float(run.realized_pnl) == expected
    assert run.legs_planned["realized_pnl_so_far"] == float(run.realized_pnl)
    assert run.closed_at is None

    # Orders placed: 2 exits + 2 new entries.
    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    assert {o.security_id for o in orders} == {_ce_id(24450), _pe_id(24450), _ce_id(24300), _pe_id(24300)}
    exit_orders = {o.security_id: o for o in orders if o.security_id in (_ce_id(24450), _pe_id(24450))}
    assert exit_orders[_ce_id(24450)].transaction_type == "BUY"  # reversing a SELL
    assert exit_orders[_ce_id(24450)].price == 40.0
    entry_orders = {o.security_id: o for o in orders if o.security_id in (_ce_id(24300), _pe_id(24300))}
    assert entry_orders[_ce_id(24300)].transaction_type == "SELL"
    assert entry_orders[_ce_id(24300)].price == 90.0


def test_apply_rolls_ignores_a_roll_with_nothing_currently_open_to_close(db_session):
    """A roll decision naming legs that are already closed (e.g. a stale
    decision computed just before a faster-firing daily SL closed
    everything) is silently skipped, not half-executed — no new legs are
    opened without their corresponding old ones actually being reversed."""
    run = _make_open_run(db_session)
    # Mark FIN1's legs already closed, as if something else beat this call to it.
    run.legs_planned = {**run.legs_planned, "leg_state": {_ce_id(24450): {"status": "closed"}, _pe_id(24450): {"status": "closed"}}}
    db_session.commit()

    dhan = MagicMock()
    new_legs = _roll_leg("FIN1", 24300, 90.0)
    decision = {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": new_legs}]}

    _apply_rolls(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    dhan.quote_data.assert_not_called()  # never even tried — nothing valid to act on
    all_security_ids = {leg["security_id"] for leg in run.legs_planned["legs"]}
    assert _ce_id(24300) not in all_security_ids  # new legs were never opened either


def test_apply_rolls_allow_empty_close_opens_new_legs_with_nothing_to_reverse(db_session):
    """Opt-in support for app.strategies.three_pair_rolling_leg_sl_target:
    a strike whose legs already exited independently (own SL/target) still
    needs its slot refilled at the roll boundary — allow_empty_close lets
    that happen with nothing actually reversed. Without the flag (previous
    test) the exact same close_security_ids/legs_planned state is silently
    skipped — this is the one narrow case where it must proceed instead."""
    run = _make_open_run(db_session)
    run.legs_planned = {**run.legs_planned, "leg_state": {_ce_id(24450): {"status": "closed"}, _pe_id(24450): {"status": "closed"}}}
    db_session.commit()

    dhan = MagicMock()
    new_legs = _roll_leg("FIN1", 24300, 90.0)
    decision = {"rolls": [{
        "close_security_ids": [_ce_id(24450), _pe_id(24450)],
        "new_legs": new_legs,
        "allow_empty_close": True,
        "leg_state_patch": {_ce_id(24450): {"in_window": False}, _pe_id(24450): {"in_window": False}},
    }]}

    _apply_rolls(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    dhan.quote_data.assert_not_called()  # nothing to reverse -> no exit-quote fetch needed
    leg_state = run.legs_planned["leg_state"]
    # The already-closed legs are untouched except for the patch (status stays "closed").
    assert leg_state[_ce_id(24450)] == {"status": "closed", "in_window": False}
    assert leg_state[_pe_id(24450)] == {"status": "closed", "in_window": False}
    # The new pair opened despite nothing being reversed.
    assert leg_state[_ce_id(24300)]["status"] == "open"
    assert leg_state[_pe_id(24300)]["status"] == "open"
    all_security_ids = {leg["security_id"] for leg in run.legs_planned["legs"]}
    assert _ce_id(24300) in all_security_ids and _pe_id(24300) in all_security_ids

    # Only the 2 new entry orders were placed — no exit orders, since there
    # was nothing open to reverse.
    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    assert {o.security_id for o in orders} == {_ce_id(24300), _pe_id(24300)}
    assert all(o.transaction_type == "SELL" for o in orders)
    assert float(run.realized_pnl or 0) == 0.0  # nothing reversed -> no P&L contribution from this roll


def test_apply_rolls_leg_state_patch_applies_even_for_legs_not_closed_this_call(db_session):
    """leg_state_patch can tag a currently-open, untouched leg too (e.g. a
    strategy trailing something unrelated to the roll itself) — mirrors
    _apply_leg_exits's existing leg_state_patch contract exactly."""
    run = _make_open_run(db_session)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0}}}},
    }
    new_legs = _roll_leg("FIN1", 24300, 90.0)
    decision = {"rolls": [{
        "close_security_ids": [_ce_id(24450), _pe_id(24450)],
        "new_legs": new_legs,
        "leg_state_patch": {_ce_id(24400): {"sl_at_cost": True}},  # FIN2's CE, untouched by this roll
    }]}

    _apply_rolls(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    leg_state = run.legs_planned["leg_state"]
    assert leg_state[_ce_id(24400)] == {"status": "open", "sl_at_cost": True}  # patched, not closed, not rolled


def test_apply_rolls_final_close_via_close_open_run_skips_rolled_away_legs(db_session):
    """After a roll, a later whole-run close (daily SL/target/end-time)
    must only reverse what's currently open — not the strikes a pair
    already rolled away from."""
    run = _make_open_run(db_session)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0}}}},
    }
    new_legs = _roll_leg("FIN1", 24300, 90.0)
    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run,
        {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": new_legs}]}, is_live=False,
    )
    db_session.refresh(run)

    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {
            _ce_id(24300): {"last_price": 50.0}, _pe_id(24300): {"last_price": 10.0},
            _ce_id(24400): {"last_price": 55.0}, _pe_id(24400): {"last_price": 50.0},
            _ce_id(24350): {"last_price": 55.0}, _pe_id(24350): {"last_price": 50.0},
        }}},
    }
    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="Daily target hit.")

    db_session.refresh(run)
    assert run.status == "closed"
    assert run.closed_at is not None

    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    # 4 from the roll (2 exit + 2 new entry) + 6 from the final close
    # (FIN1's 24300 pair + FIN2's 24400 pair + FIN3's 24350 pair) = 10.
    # Crucially, the old FIN1 24450 pair must NOT appear a second time.
    assert len([o for o in orders if o.security_id == _ce_id(24450)]) == 1
    assert len(orders) == 10
