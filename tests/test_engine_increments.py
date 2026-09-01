"""Engine-level coverage for _apply_increments -- the true incremental
scale-in mechanism app.strategies.iron_condor_rolling's untested-side
add uses: blends one new order's worth of quantity into an *existing*
open leg record (weighted-average price) instead of appending a second,
independent leg on the same contract, which would otherwise collide with
the Dashboard's (run, security_id, role) grouping the same way a hedge
and a primary leg once did (see app.routers.dashboard._pair_orders)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine.runner import _apply_increments
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg


def _pe_leg(strike: int, quantity: int = 20, price: float = 100.0) -> dict:
    return {
        "label": f"SELL {strike} PE", "security_id": "999", "trading_symbol": f"SENSEX {strike} PE 2026-09-03",
        "exchange_segment": "BSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
        "product_type": "MARGIN", "price": price, "role": "primary", "pair_id": "PE",
    }


def _make_open_run(db_session, legs: list[dict]) -> StrategyRun:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Iron Condor", code_ref="iron_condor_rolling", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True)
    db_session.add(user_strategy)
    db_session.flush()

    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={"legs": legs, "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_apply_increments_blends_quantity_and_weighted_average_price(db_session):
    run = _make_open_run(db_session, [_pe_leg(76600, quantity=20, price=100.0)])
    dhan = MagicMock()
    add_leg = OrderLeg(
        label="ADD PES SELL 76600 PE", security_id="999", trading_symbol="SENSEX 76600 PE 2026-09-03",
        exchange_segment="BSE_FNO", transaction_type="SELL", quantity=20, price=60.0, role="primary", pair_id="PE",
    )
    decision = {"increments": [{"add_leg": add_leg, "leg_state_patch": {"888": {"add_count": 1, "add_armed": False}}}]}

    _apply_increments(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    legs = run.legs_planned["legs"]
    assert len(legs) == 1  # blended in place, never a second entry
    leg = legs[0]
    assert leg["quantity"] == 40
    assert leg["price"] == (100.0 * 20 + 60.0 * 20) / 40  # == 80.0
    assert leg["was_incremented"] is True
    assert run.legs_planned["leg_state"]["888"] == {"status": "open", "add_count": 1, "add_armed": False}
    dhan.place_order.assert_not_called()  # paper mode -- no live order placed


def test_apply_increments_places_a_live_order_for_just_the_added_quantity(db_session):
    """The broker order is for the incremental quantity only (20), not the
    new blended total (40) -- the original 20 was already filled earlier."""
    run = _make_open_run(db_session, [_pe_leg(76600, quantity=20, price=100.0)])
    dhan = MagicMock()
    dhan.place_order.return_value = {"status": "success", "data": {"orderId": "abc123"}}
    add_leg = OrderLeg(
        label="ADD PES SELL 76600 PE", security_id="999", trading_symbol="SENSEX 76600 PE 2026-09-03",
        exchange_segment="BSE_FNO", transaction_type="SELL", quantity=20, price=60.0, role="primary", pair_id="PE",
    )

    _apply_increments(db_session, dhan, run.user_strategy.user_id, run, {"increments": [{"add_leg": add_leg}]}, is_live=True)

    dhan.place_order.assert_called_once()
    assert dhan.place_order.call_args.kwargs["quantity"] == 20


def test_apply_increments_skips_when_no_matching_leg(db_session):
    """No leg exists at all for that security_id/role/pair_id -- must
    never invent a position to add to."""
    run = _make_open_run(db_session, [_pe_leg(76600, quantity=20, price=100.0)])
    dhan = MagicMock()
    add_leg = OrderLeg(
        label="ADD PES SELL 77000 PE", security_id="111", trading_symbol="SENSEX 77000 PE 2026-09-03",
        exchange_segment="BSE_FNO", transaction_type="SELL", quantity=20, price=60.0, role="primary", pair_id="PE",
    )

    _apply_increments(db_session, dhan, run.user_strategy.user_id, run, {"increments": [{"add_leg": add_leg}]}, is_live=False)

    db_session.refresh(run)
    assert run.legs_planned["legs"][0]["quantity"] == 20  # untouched
    assert "was_incremented" not in run.legs_planned["legs"][0]


def test_apply_increments_skips_when_matching_leg_is_already_closed(db_session):
    run = _make_open_run(db_session, [_pe_leg(76600, quantity=20, price=100.0)])
    run.legs_planned = {**run.legs_planned, "leg_state": {"999": {"status": "closed"}}}
    db_session.commit()
    dhan = MagicMock()
    add_leg = OrderLeg(
        label="ADD PES SELL 76600 PE", security_id="999", trading_symbol="SENSEX 76600 PE 2026-09-03",
        exchange_segment="BSE_FNO", transaction_type="SELL", quantity=20, price=60.0, role="primary", pair_id="PE",
    )

    _apply_increments(db_session, dhan, run.user_strategy.user_id, run, {"increments": [{"add_leg": add_leg}]}, is_live=False)

    db_session.refresh(run)
    assert run.legs_planned["legs"][0]["quantity"] == 20  # untouched -- never adds to a closed leg
    dhan.place_order.assert_not_called()


def test_apply_increments_role_and_pair_id_must_also_match_not_just_security_id(db_session):
    """Regression for the exact scenario this mechanism exists to avoid: a
    hedge and a primary leg can legitimately share a security_id. An
    increment aimed at the primary leg must never accidentally blend into
    a same-contract hedge leg instead."""
    hedge_leg = {
        "label": "HEDGE BUY 76600 PE", "security_id": "999", "trading_symbol": "SENSEX 76600 PE 2026-09-03",
        "exchange_segment": "BSE_FNO", "transaction_type": "BUY", "quantity": 60, "order_type": "LIMIT",
        "product_type": "MARGIN", "price": 5.0, "role": "hedge", "pair_id": None,
    }
    primary_leg = _pe_leg(76600, quantity=20, price=100.0)
    run = _make_open_run(db_session, [hedge_leg, primary_leg])
    dhan = MagicMock()
    add_leg = OrderLeg(
        label="ADD PES SELL 76600 PE", security_id="999", trading_symbol="SENSEX 76600 PE 2026-09-03",
        exchange_segment="BSE_FNO", transaction_type="SELL", quantity=20, price=60.0, role="primary", pair_id="PE",
    )

    _apply_increments(db_session, dhan, run.user_strategy.user_id, run, {"increments": [{"add_leg": add_leg}]}, is_live=False)

    db_session.refresh(run)
    legs_by_role = {leg["role"]: leg for leg in run.legs_planned["legs"]}
    assert legs_by_role["hedge"]["quantity"] == 60  # untouched
    assert legs_by_role["primary"]["quantity"] == 40  # only the primary leg got the increment
