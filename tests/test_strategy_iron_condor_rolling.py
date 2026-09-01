from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.iron_condor_rolling import IronCondorRollingStrategy

IST = ZoneInfo("Asia/Kolkata")

# A fixed calendar date can't be hardcoded here — evaluate_entry() itself
# refuses to enter once "today" (real wall-clock, not the _patched_now()
# time-of-day-only mock below) has passed the configured expiry, so a
# literal past date silently turns entry tests into no-ops. Always computed
# relative to the real today instead.
FUTURE_EXPIRY = (datetime.now(IST).date() + timedelta(days=60)).isoformat()

# Strikes every 50 points from 23350 to 24650 — wide enough to cover the
# default entry (spot 24000, sell_offset 250, buy_offset 350 -> 23650..24350)
# and both roll directions (down to 23750/23850, up to 24150/24250).
_STRIKES = list(range(23350, 24651, 50))


def _row(strike: int) -> dict:
    idx = strike // 50
    return {"ce_security_id": 1000 + idx, "ce_ltp": 60.0, "pe_security_id": 2000 + idx, "pe_ltp": 55.0}


CHAIN_RESPONSE = {
    "status": "success",
    "data": {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                f"{strike}.000000": {
                    "ce": {"security_id": _row(strike)["ce_security_id"], "last_price": _row(strike)["ce_ltp"], "greeks": {}},
                    "pe": {"security_id": _row(strike)["pe_security_id"], "last_price": _row(strike)["pe_ltp"], "greeks": {}},
                }
                for strike in _STRIKES
            },
        },
    },
}


def _mock_dhan_client(spot: float = 24000.0) -> MagicMock:
    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": [FUTURE_EXPIRY]}}
    response = {**CHAIN_RESPONSE, "data": {**CHAIN_RESPONSE["data"], "data": {**CHAIN_RESPONSE["data"]["data"], "last_price": spot}}}
    dhan.option_chain.return_value = response
    dhan.ticker_data.return_value = {"status": "success", "data": {"status": "success", "data": {"IDX_I": {"13": {"last_price": spot}}}}}
    return dhan


def _within_window_time():
    return datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)


def _before_start_time():
    return datetime.now(IST).replace(hour=9, minute=0, second=0, microsecond=0)


def _ce_id(strike: int) -> str:
    return str(1000 + strike // 50)


def _pe_id(strike: int) -> str:
    return str(2000 + strike // 50)


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.iron_condor_rolling.get_lot_size", return_value=75):
        yield


def _patched_now(value):
    return patch("app.strategies.iron_condor_rolling._now_ist", return_value=value)


def _quote_response(prices: dict[str, float]) -> dict:
    return {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {sid: {"last_price": p} for sid, p in prices.items()}}}}


def _leg(strike: int, txn: str, option_type: str, pair_id: str, price: float = 100.0, quantity: int = 75) -> dict:
    sid = _ce_id(strike) if option_type == "CE" else _pe_id(strike)
    return {
        "label": f"{txn} {strike} {option_type}", "security_id": sid,
        "trading_symbol": f"NIFTY {strike} {option_type} {FUTURE_EXPIRY}", "exchange_segment": "NSE_FNO",
        "transaction_type": txn, "quantity": quantity, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "primary", "pair_id": pair_id,
    }


def _condor_notes(
    ceb: int = 24350, ces: int = 24250, pes: int = 23750, peb: int = 23650,
    ce_price: float = 50.0, pe_price: float = 45.0, **overrides,
) -> dict:
    legs = [
        _leg(ceb, "BUY", "CE", "CE", price=ce_price * 0.6),
        _leg(ces, "SELL", "CE", "CE", price=ce_price),
        _leg(pes, "SELL", "PE", "PE", price=pe_price),
        _leg(peb, "BUY", "PE", "PE", price=pe_price * 0.6),
    ]
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


# --- entry ---


def test_entry_creates_four_legs_at_the_configured_offsets():
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=0)
    strategy = IronCondorRollingStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 4
    by_label = {leg.label.split()[0]: leg for leg in legs}
    assert int(by_label["CEB"].trading_symbol.split()[1]) == 24350
    assert int(by_label["CES"].trading_symbol.split()[1]) == 24250
    assert int(by_label["PES"].trading_symbol.split()[1]) == 23750
    assert int(by_label["PEB"].trading_symbol.split()[1]) == 23650
    assert by_label["CEB"].transaction_type == "BUY" and by_label["CEB"].pair_id == "CE"
    assert by_label["CES"].transaction_type == "SELL" and by_label["CES"].pair_id == "CE"
    assert by_label["PES"].transaction_type == "SELL" and by_label["PES"].pair_id == "PE"
    assert by_label["PEB"].transaction_type == "BUY" and by_label["PEB"].pair_id == "PE"
    assert all(leg.role == "primary" for leg in legs)
    # MARGIN (carry-forward), not INTRADAY -- this position is meant to
    # survive overnight until expiry, so the broker must not auto-square it.
    assert all(leg.product_type == "MARGIN" for leg in legs)


def test_entry_blocked_when_expiry_has_already_passed():
    dhan = _mock_dhan_client()
    strategy = IronCondorRollingStrategy()
    # _within_window_time() is "today" -- an expiry dated yesterday is stale.
    yesterday = (_within_window_time().date() - timedelta(days=1)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": yesterday}, today_run_count=0)
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=0)
    strategy = IronCondorRollingStrategy()
    with _patched_now(_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=1)
    strategy = IronCondorRollingStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_returns_none_when_buy_offset_not_greater_than_sell_offset():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": FUTURE_EXPIRY, "sell_offset_points": 250, "buy_offset_points": 250},
        today_run_count=0,
    )
    strategy = IronCondorRollingStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- whole-run exit ---


def test_evaluate_exit_true_at_end_time_on_expiry_day():
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    expiry_moment = datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": expiry_moment.date().isoformat()})
    with _patched_now(expiry_moment):
        assert strategy.evaluate_exit(ctx, _condor_notes()) is True


def test_evaluate_exit_false_at_end_time_on_a_day_before_expiry():
    """This is not an intraday strategy -- reaching end_time on any day
    other than the expiry day itself must NOT force-close the position."""
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    now = datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)
    future_expiry = (now.date() + timedelta(days=3)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": future_expiry})
    with _patched_now(now):
        assert strategy.evaluate_exit(ctx, _condor_notes()) is False


def test_evaluate_exit_true_when_expiry_date_has_already_fully_passed():
    """Safety catch-up: if the expiry day itself was somehow missed, any
    later day should force-close immediately, not wait for end_time again."""
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    now = datetime.now(IST).replace(hour=9, minute=30, second=0, microsecond=0)  # well before end_time
    past_expiry = (now.date() - timedelta(days=1)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": past_expiry})
    with _patched_now(now):
        assert strategy.evaluate_exit(ctx, _condor_notes()) is True


def test_evaluate_exit_false_when_nothing_open():
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    notes = _condor_notes()
    notes["leg_state"] = {sid: {"status": "closed"} for leg in notes["legs"] for sid in [str(leg["security_id"])]}
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45"})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is False


def test_evaluate_exit_true_on_fixed_stop_loss():
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    notes = _condor_notes()
    # Heavy loss on the sold legs (CES/PES premium way up).
    dhan.quote_data.return_value = _quote_response({
        _ce_id(24350): 30.0, _ce_id(24250): 300.0, _pe_id(23750): 300.0, _pe_id(23650): 27.0,
    })
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "sl_target_mode": "fixed", "stop_loss_value": 100, "target_value": 0})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


def test_evaluate_exit_true_on_pct_target():
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    # Entry premium: CES 50 sell, PES 45 sell, CEB 30 buy, PEB 27 buy (qty 75 each).
    # Net credit = (50+45-30-27)*75 = 2850. Target 50% -> 1425 profit needed.
    notes = _condor_notes()
    dhan.quote_data.return_value = _quote_response({
        _ce_id(24350): 1.0, _ce_id(24250): 5.0, _pe_id(23750): 5.0, _pe_id(23650): 1.0,
    })
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "sl_target_mode": "pct", "stop_loss_value": 0, "target_value": 50})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


# --- rolling: only the untested side moves ---


def test_ce_side_touched_rolls_pe_side_only():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)  # spot at CEB -> roll PE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, _condor_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_pe_id(23750), _pe_id(23650)}
    new_strikes = {leg.transaction_type: int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {"SELL": 24250, "BUY": 24150}
    assert all(leg.pair_id == "PE" for leg in roll["new_legs"])
    # The CEB leg (unchanged) is flagged so this boundary doesn't re-fire.
    assert roll["leg_state_patch"] == {_ce_id(24350): {"triggered_roll": True}}


def test_pe_side_touched_rolls_ce_side_only():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=23650.0)  # spot at PEB -> roll CE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, _condor_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(24350), _ce_id(24250)}
    new_strikes = {leg.transaction_type: int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {"SELL": 23750, "BUY": 23850}
    assert all(leg.pair_id == "CE" for leg in roll["new_legs"])
    assert roll["leg_state_patch"] == {_pe_id(23650): {"triggered_roll": True}}


def test_roll_gap_is_derived_from_the_triggering_sides_own_current_width_not_a_fixed_default():
    """The gap isn't a separately configured constant — it's the triggering
    (tested) side's own current wing width. Here the CE spread is 200 wide
    (not the usual 100), so the untested PE side's new sell strike must
    land exactly on CES (24450-200=24250, i.e. CES itself), and its new
    buy strike another 200 beyond that."""
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24450.0)
    notes = _condor_notes(ceb=24450, ces=24250, pes=23750, peb=23650)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    roll = decision["rolls"][0]
    new_strikes = {leg.transaction_type: int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {"SELL": 24250, "BUY": 24050}  # gap = ceb-ces = 200, not the old fixed 100


def test_no_roll_when_spot_is_comfortably_inside_the_condor():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, _condor_notes()) is None


def test_roll_does_not_retrigger_once_the_boundary_has_already_rolled_its_opposite_side():
    """After PE has already rolled in response to this exact CEB, further
    polls with spot still sitting at/above the same CEB must not roll PE
    again."""
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_no_roll_when_not_a_clean_four_leg_condor():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    # Simulate a leg already closed independently -- not a real scenario for
    # this strategy (no per-leg exits), but evaluate_rolls must still not
    # guess when the shape isn't exactly 2+2 open legs.
    notes["leg_state"] = {_ce_id(24250): {"status": "closed"}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


# --- untested-side scale-in on a repeat touch ---


def test_second_touch_without_a_retreat_does_nothing():
    """The boundary has already rolled its opposite side once
    (triggered_roll=True) -- sitting at/above it again without ever
    retreating first must not add anything yet (not armed)."""
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_retreat_arms_for_the_next_touch_with_no_chain_fetch():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24300.0)  # comfortably below CEB (24350) -- retreated
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == []
    assert roll["new_legs"] == []
    assert roll["leg_state_patch"] == {_ce_id(24350): {"triggered_roll": True, "add_armed": True}}
    dhan.option_chain.assert_not_called()  # pure state update -- never needed a fresh chain


def test_fresh_touch_after_arming_adds_to_the_existing_pes_leg():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)  # touching CEB again, now armed
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True, "add_armed": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    assert not decision.get("rolls")  # nothing to arm/roll this time -- pure add
    inc = decision["increments"][0]
    add_leg = inc["add_leg"]
    assert add_leg.transaction_type == "SELL"
    assert add_leg.pair_id == "PE"
    assert add_leg.role == "primary"
    assert int(add_leg.trading_symbol.split()[1]) == 23750  # the current PES strike, unchanged
    assert add_leg.security_id == _pe_id(23750)
    assert add_leg.quantity == 75  # lot_size(75, patched) x lots(1)
    assert inc["leg_state_patch"] == {_ce_id(24350): {"triggered_roll": True, "add_armed": False, "add_count": 1}}


def test_add_cap_blocks_a_second_add_even_when_armed():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True, "add_armed": True, "add_count": 1}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_after_cap_reached_a_retreat_no_longer_rearms():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24300.0)  # retreated
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True, "add_count": 1}}  # cap already reached
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_pe_side_scale_in_mirrors_ce_side():
    """Same mechanic, opposite boundary: PEB already rolled the CE side
    once; a retreat then a fresh touch scales CES in further."""
    strategy = IronCondorRollingStrategy()
    notes = _condor_notes()

    # Retreat off PEB (23650) first, to arm.
    dhan_retreat = _mock_dhan_client(spot=23700.0)
    notes["leg_state"] = {_pe_id(23650): {"triggered_roll": True}}
    ctx = StrategyContext(dhan_client=dhan_retreat, params={"expiry": FUTURE_EXPIRY})
    arm_decision = strategy.evaluate_rolls(ctx, notes)
    assert arm_decision["rolls"][0]["leg_state_patch"] == {_pe_id(23650): {"triggered_roll": True, "add_armed": True}}

    # Now touch PEB again -- should add to the existing CES leg.
    dhan_touch = _mock_dhan_client(spot=23650.0)
    notes["leg_state"] = {_pe_id(23650): {"triggered_roll": True, "add_armed": True}}
    ctx = StrategyContext(dhan_client=dhan_touch, params={"expiry": FUTURE_EXPIRY})
    decision = strategy.evaluate_rolls(ctx, notes)

    inc = decision["increments"][0]
    add_leg = inc["add_leg"]
    assert add_leg.transaction_type == "SELL"
    assert add_leg.pair_id == "CE"
    assert int(add_leg.trading_symbol.split()[1]) == 24250  # the current CES strike, unchanged
    assert inc["leg_state_patch"] == {_pe_id(23650): {"triggered_roll": True, "add_armed": False, "add_count": 1}}
