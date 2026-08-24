from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.iron_condor_rolling import IronCondorRollingStrategy

IST = ZoneInfo("Asia/Kolkata")

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
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": ["2026-08-27"]}}
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
        "trading_symbol": f"NIFTY {strike} {option_type} 2026-08-27", "exchange_segment": "NSE_FNO",
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
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=0)
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


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=0)
    strategy = IronCondorRollingStrategy()
    with _patched_now(_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=1)
    strategy = IronCondorRollingStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_returns_none_when_buy_offset_not_greater_than_sell_offset():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "sell_offset_points": 250, "buy_offset_points": 250},
        today_run_count=0,
    )
    strategy = IronCondorRollingStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- whole-run exit ---


def test_evaluate_exit_true_at_end_time():
    strategy = IronCondorRollingStrategy()
    dhan = MagicMock()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45"})
    with _patched_now(datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)):
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
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})

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
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})

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
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    roll = decision["rolls"][0]
    new_strikes = {leg.transaction_type: int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {"SELL": 24250, "BUY": 24050}  # gap = ceb-ces = 200, not the old fixed 100


def test_no_roll_when_spot_is_comfortably_inside_the_condor():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24000.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})
    assert strategy.evaluate_rolls(ctx, _condor_notes()) is None


def test_roll_does_not_retrigger_once_the_boundary_has_already_rolled_its_opposite_side():
    """After PE has already rolled in response to this exact CEB, further
    polls with spot still sitting at/above the same CEB must not roll PE
    again."""
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    notes["leg_state"] = {_ce_id(24350): {"triggered_roll": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})
    assert strategy.evaluate_rolls(ctx, notes) is None


def test_no_roll_when_not_a_clean_four_leg_condor():
    strategy = IronCondorRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _condor_notes()
    # Simulate a leg already closed independently -- not a real scenario for
    # this strategy (no per-leg exits), but evaluate_rolls must still not
    # guess when the shape isn't exactly 2+2 open legs.
    notes["leg_state"] = {_ce_id(24250): {"status": "closed"}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"})
    assert strategy.evaluate_rolls(ctx, notes) is None
