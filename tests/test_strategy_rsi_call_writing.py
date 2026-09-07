from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.rsi_call_writing import (
    RSICallWritingStrategy,
    _resolve_target_expiry,
    _rsi,
    _rsi_today_and_yesterday,
)

IST = ZoneInfo("Asia/Kolkata")

_ANCHOR = datetime(2026, 9, 10, 15, 30, 0, tzinfo=IST)  # a Thursday, chosen as "today" for every test
_TODAY = _ANCHOR.date()

# Weekly-cadence expiries: today (Thursday), then +7, +14, +21 days.
_EXPIRY_TODAY = _TODAY.isoformat()
_EXPIRY_NEXT = (_TODAY + timedelta(days=7)).isoformat()
_EXPIRY_NEXT2 = (_TODAY + timedelta(days=14)).isoformat()

_STRIKES = list(range(24000, 26001, 50))


def _row(strike: int) -> dict:
    idx = strike // 50
    return {"ce_security_id": 1000 + idx, "ce_ltp": 100.0, "pe_security_id": 2000 + idx, "pe_ltp": 90.0}


def _chain_response(spot: float) -> dict:
    return {
        "status": "success",
        "data": {
            "status": "success",
            "data": {
                "last_price": spot,
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


def _mock_dhan_client(spot: float = 25000.0, expiries: list[str] | None = None, closes: list[float] | None = None) -> MagicMock:
    dhan = MagicMock()
    dhan.expiry_list.return_value = {
        "status": "success", "data": {"status": "success", "data": expiries or [_EXPIRY_NEXT, _EXPIRY_NEXT2]},
    }
    dhan.option_chain.return_value = _chain_response(spot)
    dhan.historical_daily_data.return_value = {"status": "success", "data": {"close": closes or []}}
    return dhan


def _ce_id(strike: int) -> str:
    return str(1000 + strike // 50)


def _quote_response(prices: dict[str, float]) -> dict:
    return {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {sid: {"last_price": p} for sid, p in prices.items()}}}}


def _leg(strike: int, price: float = 100.0, quantity: int = 75) -> dict:
    return {
        "label": f"SELL {strike} CE", "security_id": _ce_id(strike),
        "trading_symbol": f"NIFTY {strike} CE {_EXPIRY_NEXT}", "exchange_segment": "NSE_FNO",
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product_type": "MARGIN",
        "price": price, "role": "primary",
    }


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.rsi_call_writing.get_lot_size", return_value=75):
        yield


def _patched_now():
    return patch("app.strategies.rsi_call_writing._now_ist", return_value=_ANCHOR)


def _patched_rsi(yesterday: float | None, today: float | None):
    return patch("app.strategies.rsi_call_writing._rsi_today_and_yesterday", return_value=(yesterday, today))


# --- _rsi / _rsi_today_and_yesterday (pure math) ---


def test_rsi_none_when_not_enough_history():
    assert _rsi([100.0, 101.0], period=3) is None


def test_rsi_is_100_when_every_change_is_a_gain():
    assert _rsi([100, 101, 102, 103, 104, 105], period=3) == 100.0


def test_rsi_is_0_when_every_change_is_a_loss():
    assert _rsi([105, 104, 103, 102, 101, 100], period=3) == 0.0


def test_rsi_is_between_0_and_100_for_a_mixed_series():
    value = _rsi([100, 102, 101, 103, 99, 104, 98, 105], period=3)
    assert value is not None
    assert 0.0 < value < 100.0


def test_rsi_today_and_yesterday_differ_on_a_trending_series():
    closes = [100, 101, 102, 103, 104, 105, 106]
    yesterday, today = _rsi_today_and_yesterday(closes, period=3)
    assert yesterday == 100.0  # closes[:-1] is still monotonically rising
    assert today == 100.0


def test_rsi_today_and_yesterday_none_with_too_little_history():
    assert _rsi_today_and_yesterday([100.0], period=3) == (None, None)


# --- _resolve_target_expiry ---


def test_resolve_target_expiry_picks_nearest_future_expiry():
    assert _resolve_target_expiry([_EXPIRY_NEXT, _EXPIRY_NEXT2], _TODAY) == _EXPIRY_NEXT


def test_resolve_target_expiry_skips_today_to_next_when_today_is_expiry_day():
    assert _resolve_target_expiry([_EXPIRY_TODAY, _EXPIRY_NEXT, _EXPIRY_NEXT2], _TODAY) == _EXPIRY_NEXT


def test_resolve_target_expiry_none_when_only_todays_expiry_is_available():
    assert _resolve_target_expiry([_EXPIRY_TODAY], _TODAY) is None


def test_resolve_target_expiry_none_when_no_future_expiry_at_all():
    past = (_TODAY - timedelta(days=7)).isoformat()
    assert _resolve_target_expiry([past], _TODAY) is None


# --- evaluate_entry ---


def test_entry_blocked_before_check_time():
    dhan = _mock_dhan_client()
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY", "entry_check_time": "15:25"})
    with patch("app.strategies.rsi_call_writing._now_ist", return_value=_ANCHOR.replace(hour=10)):
        with _patched_rsi(80.0, 60.0):
            assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_when_already_traded_today():
    dhan = _mock_dhan_client()
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"}, today_run_count=1)
    with _patched_now(), _patched_rsi(80.0, 60.0):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_when_stopped_out_earlier_this_week():
    dhan = _mock_dhan_client()
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"}, week_run_count=1)
    with _patched_now(), _patched_rsi(80.0, 60.0):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_when_no_cross_down():
    dhan = _mock_dhan_client()
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"})
    with _patched_now(), _patched_rsi(60.0, 55.0):  # never was >= 70
        assert strategy.evaluate_entry(ctx) is None


def test_entry_sells_call_at_spot_plus_offset_pct_on_cross_down():
    dhan = _mock_dhan_client(spot=25000.0)
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY", "strike_offset_pct": 1.0})
    with _patched_now(), _patched_rsi(75.0, 65.0):  # crossed down through 70
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 1
    leg = legs[0]
    assert leg.transaction_type == "SELL"
    assert leg.product_type == "MARGIN"  # carried across days, not intraday
    assert leg.trading_symbol.split()[2] == "CE"
    assert leg.trading_symbol.split()[3] == _EXPIRY_NEXT
    # spot*1.01 = 25250 -> exact strike on the 50-pt grid
    assert int(leg.trading_symbol.split()[1]) == 25250


def test_entry_on_expiry_day_targets_next_weeks_expiry_not_todays():
    dhan = _mock_dhan_client(spot=25000.0, expiries=[_EXPIRY_TODAY, _EXPIRY_NEXT, _EXPIRY_NEXT2])
    strategy = RSICallWritingStrategy()
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"})
    with _patched_now(), _patched_rsi(75.0, 65.0):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert legs[0].trading_symbol.split()[3] == _EXPIRY_NEXT  # never _EXPIRY_TODAY


# --- evaluate_leg_exits: stop-loss + profit-lock ---


def _notes(strike: int = 25250, price: float = 100.0, leg_state: dict | None = None) -> dict:
    notes = {"legs": [_leg(strike, price=price)]}
    if leg_state:
        notes["leg_state"] = leg_state
    return notes


def test_leg_exit_none_when_premium_unchanged():
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 100.0})
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})
    assert strategy.evaluate_leg_exits(ctx, _notes(price=100.0)) is None


def test_leg_exit_stop_loss_fires_at_150pct_of_entry():
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 150.0})  # entry 100 -> 150% now
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})

    decision = strategy.evaluate_leg_exits(ctx, _notes(price=100.0))

    assert decision == {"close_security_ids": [_ce_id(25250)]}


def test_leg_exit_no_stop_loss_below_threshold():
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 140.0})  # under 150%
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})

    assert strategy.evaluate_leg_exits(ctx, _notes(price=100.0)) is None


def test_leg_exit_arms_profit_lock_without_closing_at_35pct_profit():
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 65.0})  # 35% decay -> profit
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})

    decision = strategy.evaluate_leg_exits(ctx, _notes(price=100.0))

    assert decision == {"close_security_ids": [], "leg_state_patch": {_ce_id(25250): {"profit_lock_armed": True}}}


def test_leg_exit_stays_armed_and_does_not_close_between_lock_levels():
    """Once armed at 65 (35% profit), premium climbing back to 80 (still
    under the 85% tightened stop) must NOT close and must NOT re-fire the
    arming patch again."""
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 80.0})
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})
    leg_state = {_ce_id(25250): {"profit_lock_armed": True}}

    assert strategy.evaluate_leg_exits(ctx, _notes(price=100.0, leg_state=leg_state)) is None


def test_leg_exit_closes_at_tightened_stop_once_armed_even_though_under_original_sl():
    """85 is well under the original 150% stop-loss level, but once the
    profit lock is armed, 85% of entry is itself the new stop."""
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = _quote_response({_ce_id(25250): 85.0})
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})
    leg_state = {_ce_id(25250): {"profit_lock_armed": True}}

    decision = strategy.evaluate_leg_exits(ctx, _notes(price=100.0, leg_state=leg_state))

    assert decision == {"close_security_ids": [_ce_id(25250)]}


def test_leg_exit_returns_none_when_quote_fetch_fails():
    strategy = RSICallWritingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "failure", "remarks": {}}
    ctx = StrategyContext(dhan_client=dhan, params={"stop_loss_pct": 50, "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85})

    assert strategy.evaluate_leg_exits(ctx, _notes(price=100.0)) is None


# --- evaluate_rolls: expiry-day roll ---


def test_no_roll_before_expiry_day():
    strategy = RSICallWritingStrategy()
    dhan = _mock_dhan_client(spot=25000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"})
    notes = {"legs": [{
        "label": "SELL 25250 CE", "security_id": _ce_id(25250), "trading_symbol": f"NIFTY 25250 CE {_EXPIRY_NEXT}",
        "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
        "product_type": "MARGIN", "price": 100.0, "role": "primary",
    }]}
    with _patched_now():
        assert strategy.evaluate_rolls(ctx, notes) is None


def test_no_roll_before_check_time_even_on_expiry_day():
    strategy = RSICallWritingStrategy()
    dhan = _mock_dhan_client(spot=25000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY"})
    notes = {"legs": [{
        "label": "SELL 25250 CE", "security_id": _ce_id(25250), "trading_symbol": f"NIFTY 25250 CE {_EXPIRY_TODAY}",
        "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
        "product_type": "MARGIN", "price": 100.0, "role": "primary",
    }]}
    with patch("app.strategies.rsi_call_writing._now_ist", return_value=_ANCHOR.replace(hour=10)):
        assert strategy.evaluate_rolls(ctx, notes) is None


def test_rolls_to_next_week_on_expiry_day():
    strategy = RSICallWritingStrategy()
    dhan = _mock_dhan_client(spot=25000.0, expiries=[_EXPIRY_TODAY, _EXPIRY_NEXT, _EXPIRY_NEXT2])
    ctx = StrategyContext(dhan_client=dhan, params={"underlying": "NIFTY", "strike_offset_pct": 1.0})
    notes = {"legs": [{
        "label": "SELL 25250 CE", "security_id": _ce_id(25250), "trading_symbol": f"NIFTY 25250 CE {_EXPIRY_TODAY}",
        "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
        "product_type": "MARGIN", "price": 100.0, "role": "primary",
    }]}
    with _patched_now():  # _ANCHOR is 15:30, at/after the default 15:25 check time
        decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    assert decision["close_security_ids"] == [_ce_id(25250)]
    new_leg = decision["new_legs"][0]
    assert new_leg.trading_symbol.split()[3] == _EXPIRY_NEXT  # rolled forward, not stayed on today's
    assert int(new_leg.trading_symbol.split()[1]) == 25250
    assert new_leg.transaction_type == "SELL"
