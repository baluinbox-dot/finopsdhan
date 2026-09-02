from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.iron_fly_adjustments import IronFlyAdjustmentsStrategy

IST = ZoneInfo("Asia/Kolkata")

# A fixed calendar date can't be hardcoded here — evaluate_entry() itself
# refuses to enter once "today" (real wall-clock, not the _patched_now()
# time-of-day-only mock below) has passed the configured expiry, so a
# literal past date silently turns entry tests into no-ops. Always computed
# relative to the real today instead.
FUTURE_EXPIRY = (datetime.now(IST).date() + timedelta(days=60)).isoformat()

# Strikes every 50 points from 23350 to 24650 — wide enough to cover the
# default entry (spot 24000, ce/pe wing offset 300 -> CEB 24300, PEB 23700)
# and both scale-in directions.
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
    with patch("app.strategies.iron_fly_adjustments.get_lot_size", return_value=75):
        yield


def _patched_now(value):
    return patch("app.strategies.iron_fly_adjustments._now_ist", return_value=value)


def _quote_response(prices: dict[str, float]) -> dict:
    return {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {sid: {"last_price": p} for sid, p in prices.items()}}}}


def _leg(strike: int, txn: str, option_type: str, pair_id: str, price: float = 100.0, quantity: int = 75) -> dict:
    sid = _ce_id(strike) if option_type == "CE" else _pe_id(strike)
    return {
        "label": f"{txn} {strike} {option_type}", "security_id": sid,
        "trading_symbol": f"NIFTY {strike} {option_type} {FUTURE_EXPIRY}", "exchange_segment": "NSE_FNO",
        "transaction_type": txn, "quantity": quantity, "order_type": "LIMIT", "product_type": "MARGIN",
        "price": price, "role": "primary", "pair_id": pair_id,
    }


def _fly_notes(
    ceb: int = 24300, ces: int = 24000, pes: int = 24000, peb: int = 23700,
    ce_price: float = 50.0, pe_price: float = 48.0, **overrides,
) -> dict:
    legs = [
        _leg(ceb, "BUY", "CE", "CE", price=ce_price * 0.5),
        _leg(ces, "SELL", "CE", "CE", price=ce_price),
        _leg(pes, "SELL", "PE", "PE", price=pe_price),
        _leg(peb, "BUY", "PE", "PE", price=pe_price * 0.5),
    ]
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


# --- entry ---


def test_entry_creates_four_legs_ces_pes_at_atm_ceb_peb_at_wing_offsets():
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=0)
    strategy = IronFlyAdjustmentsStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 4
    by_label = {leg.label.split()[0]: leg for leg in legs}
    assert int(by_label["CEB"].trading_symbol.split()[1]) == 24300
    assert int(by_label["CES"].trading_symbol.split()[1]) == 24000
    assert int(by_label["PES"].trading_symbol.split()[1]) == 24000
    assert int(by_label["PEB"].trading_symbol.split()[1]) == 23700
    assert by_label["CEB"].transaction_type == "BUY" and by_label["CEB"].pair_id == "CE"
    assert by_label["CES"].transaction_type == "SELL" and by_label["CES"].pair_id == "CE"
    assert by_label["PES"].transaction_type == "SELL" and by_label["PES"].pair_id == "PE"
    assert by_label["PEB"].transaction_type == "BUY" and by_label["PEB"].pair_id == "PE"
    assert all(leg.role == "primary" for leg in legs)
    # MARGIN (carry-forward), not INTRADAY -- this position is meant to
    # survive overnight until expiry, so the broker must not auto-square it.
    assert all(leg.product_type == "MARGIN" for leg in legs)


def test_entry_supports_asymmetric_wing_offsets():
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": FUTURE_EXPIRY, "ce_wing_offset_points": 100, "pe_wing_offset_points": 300},
        today_run_count=0,
    )
    strategy = IronFlyAdjustmentsStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    by_label = {leg.label.split()[0]: leg for leg in legs}
    assert int(by_label["CEB"].trading_symbol.split()[1]) == 24100
    assert int(by_label["PEB"].trading_symbol.split()[1]) == 23700


def test_entry_blocked_when_expiry_has_already_passed():
    dhan = _mock_dhan_client()
    strategy = IronFlyAdjustmentsStrategy()
    yesterday = (_within_window_time().date() - timedelta(days=1)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": yesterday}, today_run_count=0)
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=0)
    strategy = IronFlyAdjustmentsStrategy()
    with _patched_now(_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY}, today_run_count=1)
    strategy = IronFlyAdjustmentsStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_returns_none_when_a_wing_offset_is_zero():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": FUTURE_EXPIRY, "ce_wing_offset_points": 0, "pe_wing_offset_points": 300},
        today_run_count=0,
    )
    strategy = IronFlyAdjustmentsStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- whole-run exit ---


def test_evaluate_exit_true_at_end_time_on_expiry_day():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    expiry_moment = datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": expiry_moment.date().isoformat()})
    with _patched_now(expiry_moment):
        assert strategy.evaluate_exit(ctx, _fly_notes()) is True


def test_evaluate_exit_false_at_end_time_on_a_day_before_expiry():
    """This is not an intraday strategy -- reaching end_time on any day
    other than the expiry day itself must NOT force-close the position."""
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    now = datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)
    future_expiry = (now.date() + timedelta(days=3)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": future_expiry})
    with _patched_now(now):
        assert strategy.evaluate_exit(ctx, _fly_notes()) is False


def test_evaluate_exit_true_when_expiry_date_has_already_fully_passed():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    now = datetime.now(IST).replace(hour=9, minute=30, second=0, microsecond=0)  # well before end_time
    past_expiry = (now.date() - timedelta(days=1)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": past_expiry})
    with _patched_now(now):
        assert strategy.evaluate_exit(ctx, _fly_notes()) is True


def test_evaluate_exit_false_when_nothing_open():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    notes = _fly_notes()
    notes["leg_state"] = {sid: {"status": "closed"} for leg in notes["legs"] for sid in [str(leg["security_id"])]}
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45"})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is False


def test_evaluate_exit_true_on_fixed_stop_loss():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    notes = _fly_notes()
    # Heavy loss on the sold legs (CES/PES premium way up).
    dhan.quote_data.return_value = _quote_response({
        _ce_id(24300): 30.0, _ce_id(24000): 300.0, _pe_id(24000): 300.0, _pe_id(23700): 27.0,
    })
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "sl_target_mode": "fixed", "stop_loss_value": 100, "target_value": 0})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


def test_evaluate_exit_true_on_pct_target():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = MagicMock()
    # Entry premium: CES 50 sell, PES 48 sell, CEB 25 buy, PEB 24 buy (qty 75 each).
    # Net credit = (50+48-25-24)*75 = 3675. Target 50% -> 1837.5 profit needed.
    notes = _fly_notes()
    dhan.quote_data.return_value = _quote_response({
        _ce_id(24300): 1.0, _ce_id(24000): 5.0, _pe_id(24000): 5.0, _pe_id(23700): 1.0,
    })
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "sl_target_mode": "pct", "stop_loss_value": 0, "target_value": 50})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


# --- adjustment: scale in the untested side, at most once per side ---


def test_ce_side_touched_scales_in_one_more_pes_at_atm_without_closing_anything():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)  # spot at CEB -> scale in PE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == []
    assert roll["allow_empty_close"] is True
    assert len(roll["new_legs"]) == 1
    new_leg = roll["new_legs"][0]
    assert new_leg.transaction_type == "SELL"
    assert new_leg.pair_id == "PE"
    assert int(new_leg.trading_symbol.split()[1]) == 24000  # original ATM strike, not a new one
    assert new_leg.product_type == "MARGIN"
    # The CEB leg (unchanged) is flagged so this boundary doesn't re-fire.
    assert roll["leg_state_patch"] == {_ce_id(24300): {"scaled_in": True}}


def test_pe_side_touched_scales_in_one_more_ces_at_atm_without_closing_anything():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=23700.0)  # spot at PEB -> scale in CE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == []
    assert roll["allow_empty_close"] is True
    new_leg = roll["new_legs"][0]
    assert new_leg.transaction_type == "SELL"
    assert new_leg.pair_id == "CE"
    assert int(new_leg.trading_symbol.split()[1]) == 24000
    assert roll["leg_state_patch"] == {_pe_id(23700): {"scaled_in": True}}


def test_no_scale_in_when_spot_is_comfortably_inside_the_fly():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, _fly_notes()) is None


def test_scale_in_does_not_retrigger_once_this_ceb_has_already_scaled_in_pe():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)
    notes = _fly_notes()
    notes["leg_state"] = {_ce_id(24300): {"scaled_in": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None


# --- adjustment: configurable scale-in strike (signed offset from ATM) ---


def test_pe_scale_in_lands_above_atm_when_offset_is_positive():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)  # spot at CEB -> scale in PE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "pe_scale_in_offset_points": 100})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    new_leg = decision["rolls"][0]["new_legs"][0]
    assert new_leg.pair_id == "PE"
    assert int(new_leg.trading_symbol.split()[1]) == 24100  # ATM (24000) + 100, not the original ATM strike


def test_pe_scale_in_lands_below_atm_when_offset_is_negative():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "pe_scale_in_offset_points": -100})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    new_leg = decision["rolls"][0]["new_legs"][0]
    assert int(new_leg.trading_symbol.split()[1]) == 23900  # ATM (24000) - 100


def test_ce_scale_in_lands_at_the_configured_offset():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=23700.0)  # spot at PEB -> scale in CE side
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "ce_scale_in_offset_points": -100})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    new_leg = decision["rolls"][0]["new_legs"][0]
    assert new_leg.pair_id == "CE"
    assert int(new_leg.trading_symbol.split()[1]) == 23900  # ATM (24000) - 100, still a CE strike below ATM


def test_scale_in_offset_of_zero_still_lands_on_the_original_atm_strike():
    """Explicit regression for the pre-existing default behavior — 0 must
    still mean "the original ATM strike", not "no scale-in"."""
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "pe_scale_in_offset_points": 0})

    decision = strategy.evaluate_rolls(ctx, _fly_notes())

    assert decision is not None
    new_leg = decision["rolls"][0]["new_legs"][0]
    assert int(new_leg.trading_symbol.split()[1]) == 24000


def test_scale_in_still_works_when_pe_side_already_has_two_open_sell_legs():
    """PES having two open legs (from an earlier scale-in) must not by
    itself block a further scale-in -- only the CEB `scaled_in` flag does."""
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)
    notes = _fly_notes()
    notes["legs"] = notes["legs"] + [_leg(24000, "SELL", "PE", "PE", price=48.0)]
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    # Not yet flagged -- still scales in again (guard lives on CEB's leg_state).
    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is not None
    assert int(decision["rolls"][0]["new_legs"][0].trading_symbol.split()[1]) == 24000


def test_no_scale_in_when_more_than_one_wing_leg_is_open_on_a_side():
    strategy = IronFlyAdjustmentsStrategy()
    dhan = _mock_dhan_client(spot=24300.0)
    notes = _fly_notes()
    # Simulate a malformed state with two open CEB legs -- evaluate_rolls
    # must not guess when the shape isn't exactly one wing per side.
    notes["legs"] = notes["legs"] + [_leg(24350, "BUY", "CE", "CE", price=20.0)]
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY})
    assert strategy.evaluate_rolls(ctx, notes) is None
