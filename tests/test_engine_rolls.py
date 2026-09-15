"""Engine-level coverage for the roll machinery (_apply_rolls /
Strategy.evaluate_rolls) that app/strategies/three_pair_rolling.py depends
on — closing a group of legs and opening its replacement within one still-
open run, without touching any other leg."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine import runner
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

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

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


def test_apply_rolls_skips_roll_when_a_fresh_exit_quote_is_unavailable(db_session):
    """A quote fetch that fails (Dhan throttled/errored, or the security is
    simply missing from the response) must never fall back to the stale
    entry price — that fakes a flat/no-op fill instead of a real market
    price. The roll should be skipped entirely (nothing closed, nothing
    new opened) so it can retry with a fresh quote next poll."""
    run = _make_open_run(db_session)
    dhan = MagicMock()
    # Quote for the CE leg is present, but the PE leg is absent from the
    # response — a partial failure, not a clean success or a clean error.
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}}}},
    }

    new_legs = _roll_leg("FIN1", 24300, 90.0)
    decision = {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": new_legs}]}

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

    db_session.refresh(run)
    assert run.legs_planned.get("leg_state") is None  # nothing touched at all
    all_security_ids = {leg["security_id"] for leg in run.legs_planned["legs"]}
    assert _ce_id(24300) not in all_security_ids  # new pair was never opened either
    assert float(run.realized_pnl or 0) == 0.0

    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    assert orders == []  # no exit and no entry orders placed


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

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

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

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

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

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

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
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
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


def test_a_revisited_security_id_is_only_reversed_once_on_final_close(db_session):
    """Regression: spot can roll a strike away and later roll right back to
    it (e.g. FIN1 shifts 24450 -> 24300 -> back to 24450), giving that one
    security_id *two* entries in legs_planned["legs"] history -- the first
    closed, the second genuinely open. leg_state only tracks each
    security_id's latest status, so before app.strategies.base.
    currently_open_legs existed, a raw "status != closed" filter matched
    BOTH history entries once the strike came back (the second roll's
    leg_state[sid] = {"status": "open"} clobbers the earlier "closed"
    record) -- _close_open_run then placed *two* exit orders for one
    physical leg, corrupting realized P&L and (in live mode) sending a
    real duplicate reversing order. Confirmed live on 4 production
    positions 2026-09-02 before this fix (all paper mode)."""
    run = _make_open_run(db_session)
    dhan = MagicMock()

    # Roll 1: FIN1 24450 -> 24300 (closes 24450, opens 24300).
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0}}}},
    }
    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run,
        {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": _roll_leg("FIN1", 24300, 90.0)}]},
        is_live=False, user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )
    db_session.refresh(run)

    # Roll 2: spot reverses -- FIN1 24300 -> back to 24450 (the exact same
    # security_id closed by Roll 1, given a fresh entry price this time).
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24300): {"last_price": 95.0}, _pe_id(24300): {"last_price": 12.0}}}},
    }
    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run,
        {"rolls": [{"close_security_ids": [_ce_id(24300), _pe_id(24300)], "new_legs": _roll_leg("FIN1", 24450, 65.0)}]},
        is_live=False, user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )
    db_session.refresh(run)

    # legs_planned["legs"] now holds the 24450 security_ids twice: the
    # original (closed by Roll 1) and the reopened one (from Roll 2).
    all_24450_entries = [leg for leg in run.legs_planned["legs"] if leg["security_id"] in (_ce_id(24450), _pe_id(24450))]
    assert len(all_24450_entries) == 4  # 2 security_ids x 2 history entries each
    assert run.legs_planned["leg_state"][_ce_id(24450)]["status"] == "open"  # the reopened one is the current truth

    # Whole-run close (daily SL/target/end-time) must reverse the reopened
    # 24450 pair exactly once each -- not the stale, already-closed entry too.
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {
            _ce_id(24450): {"last_price": 50.0}, _pe_id(24450): {"last_price": 20.0},
            _ce_id(24400): {"last_price": 55.0}, _pe_id(24400): {"last_price": 50.0},
            _ce_id(24350): {"last_price": 55.0}, _pe_id(24350): {"last_price": 50.0},
        }}},
    }
    _close_open_run(db_session, dhan, run.user_strategy.user_id, run, is_live=False, reason="Daily target hit.")

    db_session.refresh(run)
    assert run.status == "closed"

    orders = db_session.query(Order).filter(Order.strategy_run_id == run.id).all()
    # 4 from Roll 1 (2 exit + 2 entry) + 4 from Roll 2 (2 exit + 2 entry) +
    # 6 from the final close (FIN1's reopened 24450 pair + FIN2's 24400 +
    # FIN3's 24350) = 14. The reopened 24450 pair must be reversed exactly
    # once each by the final close, not twice.
    final_close_24450_exits = [
        o for o in orders
        if o.security_id in (_ce_id(24450), _pe_id(24450)) and o.transaction_type == "BUY" and o.price in (50.0, 20.0)
    ]
    assert len(final_close_24450_exits) == 2  # one CE, one PE -- not four
    assert len(orders) == 14


# --- roll alert emails (Balu's request: an email for every roll, not just entry/close) ---


def _capture_emails(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(
        runner, "send_email",
        lambda to_email, subject, *, html_body, text_body: sent.append((to_email, subject, html_body, text_body)),
    )
    return sent


def test_apply_rolls_sends_one_alert_email_naming_the_closed_and_new_legs(db_session, monkeypatch):
    run = _make_open_run(db_session)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {_ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0}}}},
    }
    new_legs = _roll_leg("FIN1", 24300, 90.0)
    new_legs[1].price = 15.0
    decision = {"rolls": [{"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": new_legs}]}

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

    assert len(sent) == 1
    to_email, subject, html_body, text_body = sent[0]
    assert to_email == "trader@example.com"
    assert "Rolled" in subject and "3-Pair Rolling" in subject and "PAPER" in subject
    # Closed leg (old strike, reversed at the fresh exit quote) and new leg
    # (new strike, at its own entry price) both named in the body.
    assert "NIFTY 24450 CE" in text_body and "BUY 75" in text_body  # closing a SELL reverses to BUY
    assert "NIFTY 24300 CE" in text_body and "SELL 75" in text_body
    # P&L: SELL 60 -> bought back at 40 (profit) + SELL 60 -> bought back at 70 (loss).
    expected_pnl = (60.0 - 40.0) * 75 + (60.0 - 70.0) * 75
    assert f"{expected_pnl:,.0f}" in text_body


def test_apply_rolls_sends_a_separate_alert_email_per_roll_in_one_decision(db_session, monkeypatch):
    """A single evaluate_rolls pass can return more than one roll (e.g. a
    big spot gap producing two sequential T/M/B shifts, or a T/M/B shift
    and a hedge re-entry in the same poll) -- each must get its own email,
    not one combined one."""
    run = _make_open_run(db_session)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {
            _ce_id(24450): {"last_price": 40.0}, _pe_id(24450): {"last_price": 70.0},
            _ce_id(24400): {"last_price": 45.0}, _pe_id(24400): {"last_price": 65.0},
        }}},
    }
    decision = {
        "rolls": [
            {"close_security_ids": [_ce_id(24450), _pe_id(24450)], "new_legs": _roll_leg("FIN1", 24300, 90.0)},
            {"close_security_ids": [_ce_id(24400), _pe_id(24400)], "new_legs": _roll_leg("FIN2", 24250, 95.0)},
        ]
    }

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

    assert len(sent) == 2
    bodies = [s[3] for s in sent]
    assert any("NIFTY 24450 CE" in b and "NIFTY 24300 CE" in b for b in bodies)
    assert any("NIFTY 24400 CE" in b and "NIFTY 24250 CE" in b for b in bodies)


def test_apply_rolls_alert_says_none_closed_when_the_roll_had_nothing_to_reverse(db_session, monkeypatch):
    """allow_empty_close (used by the per-leg SL/target sibling strategy
    when both of a strike's legs already exited independently before the
    roll boundary) still opens a fresh pair and must still alert -- the
    email should say so instead of listing a phantom closed leg."""
    run = _make_open_run(db_session)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    decision = {"rolls": [{
        "close_security_ids": [], "new_legs": _roll_leg("FIN1", 24300, 90.0), "allow_empty_close": True,
    }]}

    _apply_rolls(
        db_session, dhan, run.user_strategy.user_id, run, decision, is_live=False,
        user=run.user_strategy.user, strategy_name="3-Pair Rolling",
    )

    assert len(sent) == 1
    text_body = sent[0][3]
    assert "already closed earlier" in text_body
    assert "NIFTY 24300 CE" in text_body
    assert "n/a" in text_body  # no realized P&L to report -- nothing was actually closed this roll
