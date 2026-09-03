from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.dynamic_strangle import DynamicStrangleStrategy

IST = ZoneInfo("Asia/Kolkata")

FUTURE_EXPIRY = (datetime.now(IST).date() + timedelta(days=60)).isoformat()

# Strikes every 500 points, wide enough to cover Balu's own worked example
# (spot 57000, base distance 2000 -> CE 59000 / PE 55000, adjustment 1000,
# fresh strangle 500) in both the UP and DOWN directions.
_STRIKES = list(range(50000, 65001, 500))


def _row(strike: int) -> dict:
    idx = strike // 500
    return {"ce_security_id": 1000 + idx, "ce_ltp": 60.0, "pe_security_id": 2000 + idx, "pe_ltp": 55.0}


CHAIN_RESPONSE = {
    "status": "success",
    "data": {
        "status": "success",
        "data": {
            "last_price": 57000.0,
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


def _mock_dhan_client(spot: float = 57000.0) -> MagicMock:
    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": [FUTURE_EXPIRY]}}
    response = {**CHAIN_RESPONSE, "data": {**CHAIN_RESPONSE["data"], "data": {**CHAIN_RESPONSE["data"]["data"], "last_price": spot}}}
    dhan.option_chain.return_value = response
    dhan.ticker_data.return_value = {"status": "success", "data": {"status": "success", "data": {"IDX_I": {"25": {"last_price": spot}}}}}
    return dhan


def _within_window_time():
    return datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)


def _before_start_time():
    return datetime.now(IST).replace(hour=9, minute=0, second=0, microsecond=0)


def _ce_id(strike: int) -> str:
    return str(1000 + strike // 500)


def _pe_id(strike: int) -> str:
    return str(2000 + strike // 500)


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.dynamic_strangle.get_lot_size", return_value=25):
        yield


def _patched_now(value):
    return patch("app.strategies.dynamic_strangle._now_ist", return_value=value)


def _quote_response(prices: dict[str, float]) -> dict:
    return {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {sid: {"last_price": p} for sid, p in prices.items()}}}}


def _leg(strike: int, option_type: str, price: float = 100.0, quantity: int = 25) -> dict:
    sid = _ce_id(strike) if option_type == "CE" else _pe_id(strike)
    return {
        "label": f"SELL {strike} {option_type}", "security_id": sid,
        "trading_symbol": f"BANKNIFTY {strike} {option_type} {FUTURE_EXPIRY}", "exchange_segment": "NSE_FNO",
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "primary",
    }


def _strangle_notes(ce: int = 59000, pe: int = 55000, ce_price: float = 60.0, pe_price: float = 55.0, **overrides) -> dict:
    legs = [_leg(ce, "CE", price=ce_price), _leg(pe, "PE", price=pe_price)]
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


# --- entry ---


def test_entry_sells_ce_and_pe_at_spot_plus_minus_base_distance():
    dhan = _mock_dhan_client(spot=57000.0)
    ctx = StrategyContext(
        dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000}, today_run_count=0,
    )
    strategy = DynamicStrangleStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 2
    by_type = {leg.trading_symbol.split()[2]: leg for leg in legs}
    assert int(by_type["CE"].trading_symbol.split()[1]) == 59000
    assert int(by_type["PE"].trading_symbol.split()[1]) == 55000
    assert all(leg.transaction_type == "SELL" for leg in legs)
    assert all(leg.product_type == "INTRADAY" for leg in legs)  # daily square-off, never carried overnight
    assert all(leg.role == "primary" for leg in legs)


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY"}, today_run_count=0)
    strategy = DynamicStrangleStrategy()
    with _patched_now(_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY"}, today_run_count=1)
    strategy = DynamicStrangleStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_returns_none_when_base_distance_is_zero():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 0}, today_run_count=0,
    )
    strategy = DynamicStrangleStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- whole-run exit ---


def test_evaluate_exit_true_at_end_time_every_day_not_just_expiry_day():
    """Genuinely intraday -- unlike Iron Condor Rolling / Iron Fly, this
    force-closes at end_time on ANY day, not only the expiry day."""
    strategy = DynamicStrangleStrategy()
    dhan = MagicMock()
    now = datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)
    far_expiry = (now.date() + timedelta(days=10)).isoformat()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "expiry": far_expiry})
    with _patched_now(now):
        assert strategy.evaluate_exit(ctx, _strangle_notes()) is True


def test_evaluate_exit_false_before_end_time_with_no_sl_target_hit():
    strategy = DynamicStrangleStrategy()
    dhan = MagicMock()
    notes = _strangle_notes()
    dhan.quote_data.return_value = _quote_response({_ce_id(59000): 60.0, _pe_id(55000): 55.0})
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "daily_stop_loss": 10000, "daily_target": 15000})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is False


def test_evaluate_exit_true_on_daily_stop_loss():
    strategy = DynamicStrangleStrategy()
    dhan = MagicMock()
    notes = _strangle_notes()
    # Both legs' premium has risen sharply against the seller.
    dhan.quote_data.return_value = _quote_response({_ce_id(59000): 300.0, _pe_id(55000): 300.0})
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "daily_stop_loss": 100, "daily_target": 0})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


# --- adjustment / reset ---


def test_up_move_adjusts_pe_only_ce_untouched():
    """Balu's worked example: spot 57000 -> 58000 with base 2000 (adjustment
    1000) moves PE from 55000 to 56000; CE stays at 59000."""
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=58000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})

    decision = strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000))

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == [_pe_id(55000)]
    assert len(roll["new_legs"]) == 1
    new_leg = roll["new_legs"][0]
    assert new_leg.transaction_type == "SELL"
    assert int(new_leg.trading_symbol.split()[1]) == 56000
    assert new_leg.trading_symbol.split()[2] == "PE"
    assert roll["leg_state_patch"] == {_ce_id(59000): {"adjusted": True}}


def test_up_move_resets_both_legs_once_spot_reaches_ce_strike():
    """Spot reaching 59000 (== the current CE strike) closes both legs and
    opens a fresh strangle around spot at the fresh distance (500): 59500 CE / 58500 PE."""
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=59000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})

    decision = strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000))

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(59000), _pe_id(55000)}
    by_type = {leg.trading_symbol.split()[2]: leg for leg in roll["new_legs"]}
    assert int(by_type["CE"].trading_symbol.split()[1]) == 59500
    assert int(by_type["PE"].trading_symbol.split()[1]) == 58500
    assert all(leg.transaction_type == "SELL" for leg in roll["new_legs"])
    assert "leg_state_patch" not in roll


def test_down_move_adjusts_ce_only_pe_untouched():
    """Mirror of the UP case: spot 57000 -> 56000 moves CE from 59000 down
    to 58000; PE stays at 55000."""
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=56000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})

    decision = strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000))

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == [_ce_id(59000)]
    new_leg = roll["new_legs"][0]
    assert int(new_leg.trading_symbol.split()[1]) == 58000
    assert new_leg.trading_symbol.split()[2] == "CE"
    assert roll["leg_state_patch"] == {_pe_id(55000): {"adjusted": True}}


def test_down_move_resets_both_legs_once_spot_reaches_pe_strike():
    """Spot falling to 55000 (== the current PE strike) resets around spot:
    55500 CE / 54500 PE."""
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=55000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})

    decision = strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000))

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(59000), _pe_id(55000)}
    by_type = {leg.trading_symbol.split()[2]: leg for leg in roll["new_legs"]}
    assert int(by_type["CE"].trading_symbol.split()[1]) == 55500
    assert int(by_type["PE"].trading_symbol.split()[1]) == 54500


def test_no_roll_when_spot_is_comfortably_inside_the_strangle():
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=57000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})
    assert strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000)) is None


def test_up_adjust_does_not_retrigger_once_this_ce_has_already_adjusted_pe():
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=58000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})
    notes = _strangle_notes(ce=59000, pe=55000)
    notes["leg_state"] = {_ce_id(59000): {"adjusted": True}}
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_reset_takes_priority_over_adjust_when_spot_jumps_straight_past_ce():
    """A big single-poll move landing well beyond CE must reset (recenter on
    live spot), not fall back to the intermediate 'move PE' adjustment."""
    strategy = DynamicStrangleStrategy()
    dhan = _mock_dhan_client(spot=61000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": FUTURE_EXPIRY, "underlying": "BANKNIFTY", "base_distance_points": 2000})

    decision = strategy.evaluate_rolls(ctx, _strangle_notes(ce=59000, pe=55000))

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(59000), _pe_id(55000)}
    by_type = {leg.trading_symbol.split()[2]: leg for leg in roll["new_legs"]}
    # Recentered on live spot (61000), not the old CE strike (59000).
    assert int(by_type["CE"].trading_symbol.split()[1]) == 61500
    assert int(by_type["PE"].trading_symbol.split()[1]) == 60500
