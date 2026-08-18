"""Engine-level coverage for the per-leg exit machinery (`evaluate_leg_exits`
/ `_apply_leg_exits` / `_close_open_run`'s leg_state filtering) — the part of
the engine that app/strategies/atm_straddle_trigger_hedge.py depends on but
that no strategy before it needed."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.engine.runner import _apply_leg_exits, _close_open_run
from app.models import Order, Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy


def _make_open_run(db_session) -> StrategyRun:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="ATM Straddle", code_ref="atm_straddle_trigger_hedge", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True)
    db_session.add(user_strategy)
    db_session.flush()

    legs = [
        {"label": "SELL ATM 24000 CE", "security_id": "81", "trading_symbol": "NIFTY 24000 CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 60.0, "role": "primary"},
        {"label": "SELL ATM 24000 PE", "security_id": "31", "trading_symbol": "NIFTY 24000 PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 55.0, "role": "primary"},
        {"label": "HEDGE BUY 24200 CE", "security_id": "91", "trading_symbol": "NIFTY 24200 CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 8.0, "role": "hedge"},
        {"label": "HEDGE BUY 23800 PE", "security_id": "21", "trading_symbol": "NIFTY 23800 PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 10.0, "role": "hedge"},
    ]
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={"legs": legs, "entry_premium": 60.0 + 55.0 - 8.0 - 10.0, "params_snapshot": {}},
    )
    db_session.add(run)
    db_session.commit()
    return run


def _fresh_quote(security_id: str, price: float) -> dict:
    return {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {security_id: {"last_price": price}}}},
    }


def test_apply_leg_exits_closes_only_named_legs_and_persists_state(db_session):
    run = _make_open_run(db_session)
    dhan = MagicMock()
    dhan.quote_data.return_value = _fresh_quote("81", 82.0)  # CE exit fill price

    decision = {"close_security_ids": ["81", "91"], "leg_state_patch": {"31": {"sl_moved_to_cost": True}}}
    _apply_leg_exits(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    assert run.status == "open"  # PE (primary) still open -> run stays open
    leg_state = run.legs_planned["leg_state"]
    assert leg_state["81"]["status"] == "closed"
    assert leg_state["91"]["status"] == "closed"
    assert leg_state["31"] == {"status": "open", "sl_moved_to_cost": True}  # patch applied even though PE wasn't closed
    assert "21" not in leg_state  # untouched hedge leg has no state yet (implicitly open)

    # Two EXIT orders were placed: the CE sell reversal and the CE hedge reversal.
    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    exit_orders = {o.security_id: o for o in orders}
    assert set(exit_orders) == {"81", "91"}
    assert exit_orders["81"].transaction_type == "BUY"  # reversing a SELL
    assert exit_orders["81"].price == 82.0
    assert exit_orders["91"].transaction_type == "SELL"  # reversing a BUY hedge

    # Realized P&L only reflects the two legs actually closed this pass:
    # CE sell 60 -> bought back 82 (loss). The CE hedge (91) has no fresh
    # quote in this mock, so it falls back to its own entry price (8.0) as
    # its exit price -> zero P&L contribution, not a guessed number.
    expected = (60.0 - 82.0) * 75 + 0.0
    assert float(run.realized_pnl) == expected
    assert run.closed_at is None  # run still open (PE leg remains) -> not closed yet


def test_apply_leg_exits_closes_run_when_last_primary_leg_closes(db_session):
    run = _make_open_run(db_session)
    # Simulate CE already closed by an earlier pass.
    run.legs_planned = {**run.legs_planned, "leg_state": {"81": {"status": "closed"}, "91": {"status": "closed"}}}
    db_session.commit()

    dhan = MagicMock()
    dhan.quote_data.return_value = _fresh_quote("31", 5.0)

    decision = {"close_security_ids": ["31", "21"], "leg_state_patch": {}}
    _apply_leg_exits(db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False)

    db_session.refresh(run)
    assert run.status == "closed"
    leg_state = run.legs_planned["leg_state"]
    assert all(v["status"] == "closed" for v in leg_state.values())

    # PE sell 55 -> bought back 5 (profit); PE hedge (21) has no fresh
    # quote here, falls back to its own entry price -> zero contribution.
    assert float(run.realized_pnl) == (55.0 - 5.0) * 75
    assert run.closed_at is not None


def test_close_open_run_skips_legs_already_closed_via_leg_exit(db_session):
    run = _make_open_run(db_session)
    run.legs_planned = {**run.legs_planned, "leg_state": {"81": {"status": "closed"}, "91": {"status": "closed"}}}
    db_session.commit()

    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"31": {"last_price": 5.0}, "21": {"last_price": 1.0}}}},
    }

    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="target hit")

    db_session.refresh(run)
    assert run.status == "closed"
    # Only the two legs that were still open (PE sell + its hedge) get reversed —
    # the already-closed CE sell/hedge must not be reversed a second time.
    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    assert {o.security_id for o in orders} == {"31", "21"}

    # PE sell 55 -> 5 (profit) + PE hedge buy 10 -> 1 (loss); the already-closed
    # CE legs contribute nothing since _close_open_run skips them entirely.
    expected = (55.0 - 5.0) * 75 + (1.0 - 10.0) * 75
    assert float(run.realized_pnl) == expected
    assert run.closed_at is not None


def test_realized_pnl_accumulates_across_two_partial_exit_passes(db_session):
    """A strategy with independent per-leg exits can close a run across
    more than one scheduler pass (e.g. CE SL's out now, PE closes later
    on target) — realized_pnl must accumulate across both, not overwrite."""
    run = _make_open_run(db_session)
    dhan = MagicMock()

    # Pass 1: CE leg + its hedge close.
    dhan.quote_data.return_value = _fresh_quote("81", 82.0)
    _apply_leg_exits(
        db_session, dhan, run.user_strategy.user_id, run,
        {"close_security_ids": ["81", "91"], "leg_state_patch": {}}, is_live=False,
    )
    db_session.refresh(run)
    first_pass_pnl = float(run.realized_pnl)
    assert first_pass_pnl == (60.0 - 82.0) * 75  # CE hedge (91) has no quote here -> 0 contribution
    assert run.status == "open"  # PE leg still open

    # Pass 2: PE leg + its hedge close, finishing the run.
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"31": {"last_price": 5.0}, "21": {"last_price": 1.0}}}},
    }
    _apply_leg_exits(
        db_session, dhan, run.user_strategy.user_id, run,
        {"close_security_ids": ["31", "21"], "leg_state_patch": {}}, is_live=False,
    )
    db_session.refresh(run)

    second_pass_pnl = (55.0 - 5.0) * 75 + (1.0 - 10.0) * 75
    assert float(run.realized_pnl) == first_pass_pnl + second_pass_pnl
    assert run.status == "closed"
    assert run.closed_at is not None


def test_close_open_run_backward_compatible_with_no_leg_state(db_session):
    """Strategies that never populate leg_state (every strategy before this
    one) must still see every leg reversed, exactly as before."""
    run = _make_open_run(db_session)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {
            "81": {"last_price": 1.0}, "31": {"last_price": 1.0}, "91": {"last_price": 1.0}, "21": {"last_price": 1.0},
        }}},
    }

    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="window end")

    db_session.refresh(run)
    assert run.status == "closed"
    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    assert {o.security_id for o in orders} == {"81", "31", "91", "21"}

    # All four legs exit at 1.0: two sells profit, two hedge buys lose.
    expected = (60.0 - 1.0) * 75 + (55.0 - 1.0) * 75 + (1.0 - 8.0) * 75 + (1.0 - 10.0) * 75
    assert float(run.realized_pnl) == expected
    assert run.closed_at is not None
