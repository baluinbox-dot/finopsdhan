from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.three_pair_rolling_leg_sl_target import ThreePairRollingLegSLTargetStrategy

IST = ZoneInfo("Asia/Kolkata")

# Strikes every 50 points from 24100 to 24700, spot = 24400.
_STRIKES = list(range(24100, 24701, 50))


def _row(strike: int) -> dict:
    idx = strike // 50
    return {"ce_security_id": 1000 + idx, "ce_ltp": 60.0, "pe_security_id": 2000 + idx, "pe_ltp": 55.0}


CHAIN_RESPONSE = {
    "status": "success",
    "data": {
        "status": "success",
        "data": {
            "last_price": 24400.0,
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


def _mock_dhan_client(spot: float = 24400.0) -> MagicMock:
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
    with patch("app.strategies.three_pair_rolling_leg_sl_target.get_lot_size", return_value=75):
        yield


def _patched_now(value):
    return patch("app.strategies.three_pair_rolling_leg_sl_target._now_ist", return_value=value)


# --- entry ---


def test_entry_creates_a_t_m_b_window_with_no_hedge():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0)
    strategy = ThreePairRollingLegSLTargetStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 6  # B/M/T x (CE + PE), no hedge
    assert {leg.transaction_type for leg in legs} == {"SELL"}
    assert all(leg.role == "primary" for leg in legs)


def test_entry_tags_legs_with_the_underlyings_own_derivative_segment():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan, params={"underlying": "SENSEX", "expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0,
    )
    strategy = ThreePairRollingLegSLTargetStrategy()
    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)
    assert legs is not None
    assert {leg.exchange_segment for leg in legs} == {"BSE_FNO"}


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=0)
    strategy = ThreePairRollingLegSLTargetStrategy()
    with _patched_now(_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=1)
    strategy = ThreePairRollingLegSLTargetStrategy()
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- entry: hedge (same mechanics as the original strategy) ---


def test_entry_with_hedge_adds_one_ce_and_one_pe_sized_for_all_three_pairs():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 1, "hedge_enabled": True, "hedge_premium_target": 5},
        today_run_count=0,
    )
    strategy = ThreePairRollingLegSLTargetStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 8  # 3 pairs x (CE+PE) + 1 CE hedge + 1 PE hedge

    primary = [leg for leg in legs if leg.role == "primary"]
    hedges = [leg for leg in legs if leg.role == "hedge"]
    assert len(primary) == 6
    assert len(hedges) == 2
    assert {leg.transaction_type for leg in hedges} == {"BUY"}
    assert {leg.trading_symbol.split()[2] for leg in hedges} == {"CE", "PE"}

    for leg in hedges:
        assert leg.quantity == 75 * 1 * 3  # 1 lot/pair x 3 pairs
    for leg in primary:
        assert leg.quantity == 75 * 1


def test_entry_with_hedge_scales_3x_with_lots_per_pair():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 2, "hedge_enabled": True, "hedge_premium_target": 5},
        today_run_count=0,
    )
    strategy = ThreePairRollingLegSLTargetStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    hedges = [leg for leg in legs if leg.role == "hedge"]
    primary = [leg for leg in legs if leg.role == "primary"]
    for leg in hedges:
        assert leg.quantity == 75 * 2 * 3
    for leg in primary:
        assert leg.quantity == 75 * 2


def test_entry_hedge_disabled_by_default_adds_no_hedge_legs():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 1}, today_run_count=0)
    strategy = ThreePairRollingLegSLTargetStrategy()

    with _patched_now(_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 6
    assert all(leg.role == "primary" for leg in legs)


def test_hedge_legs_are_immune_to_per_leg_sl_target():
    """A hedge is a BUY, not a SELL — the per-leg SL/target math (built for
    the SELL sign convention) must never even look at it. Hedge legs only
    ever close via the whole-run close path."""
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    # Wildly move the hedge's own quote — if evaluate_leg_exits touched it
    # at all, this would misfire or raise on the SELL-only math.
    dhan.quote_data.return_value = _quote_response({_ce_id(24200): 1.0, _pe_id(24600): 1.0})
    notes = _window_notes()
    notes["legs"] = notes["legs"] + [
        {"label": "HEDGE BUY 24200 CE", "security_id": _ce_id(24200), "trading_symbol": "NIFTY 24200 CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 225, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 5.0, "role": "hedge"},
        {"label": "HEDGE BUY 24600 PE", "security_id": _pe_id(24600), "trading_symbol": "NIFTY 24600 PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 225, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 5.0, "role": "hedge"},
    ]
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25, "leg_target_pct": 80})

    decision = strategy.evaluate_leg_exits(ctx, notes)
    assert decision is None  # nothing hit SL/target among the primary legs, and hedge was never considered


# --- helpers for leg-exit / roll tests ---


def _strike_legs(strike: int, role: str = "T", ce_price: float = 100.0, pe_price: float = 100.0, quantity: int = 75) -> list[dict]:
    return [
        {"label": f"{role} SELL {strike} CE", "security_id": _ce_id(strike), "trading_symbol": f"NIFTY {strike} CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": ce_price, "role": "primary"},
        {"label": f"{role} SELL {strike} PE", "security_id": _pe_id(strike), "trading_symbol": f"NIFTY {strike} PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": pe_price, "role": "primary"},
    ]


def _window_notes(b: int = 24350, m: int = 24400, t: int = 24450, **overrides) -> dict:
    legs = _strike_legs(b, "B") + _strike_legs(m, "M") + _strike_legs(t, "T")
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


def _quote_response(prices: dict[str, float]) -> dict:
    return {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {sid: {"last_price": p} for sid, p in prices.items()}}}}


# --- per-leg stop-loss / target ---


def test_ce_leg_sl_closes_only_ce_and_moves_pe_to_cost():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    # CE entry 100, SL 25% -> SL price 125. Current CE premium 130 -> hit.
    # PE entry 100, current premium 90 -> nowhere near target(80%=20) or SL.
    dhan.quote_data.return_value = _quote_response({_ce_id(24450): 130.0, _pe_id(24450): 90.0})
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25, "leg_target_pct": 80})

    decision = strategy.evaluate_leg_exits(ctx, _window_notes())

    assert decision is not None
    assert decision["close_security_ids"] == [_ce_id(24450)]
    patch_ = decision["leg_state_patch"]
    assert patch_[_ce_id(24450)] == {"closed_reason": "leg_sl"}
    assert patch_[_pe_id(24450)] == {"sl_at_cost": True}


def test_leg_target_closes_only_that_leg_no_cost_trail_on_sibling():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    # CE entry 100, target 80% -> target price 20. Current CE premium 18 -> hit.
    # PE entry 100, current premium 95 -> untouched.
    dhan.quote_data.return_value = _quote_response({_ce_id(24450): 18.0, _pe_id(24450): 95.0})
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25, "leg_target_pct": 80})

    decision = strategy.evaluate_leg_exits(ctx, _window_notes())

    assert decision is not None
    assert decision["close_security_ids"] == [_ce_id(24450)]
    patch_ = decision["leg_state_patch"]
    assert patch_[_ce_id(24450)] == {"closed_reason": "leg_target"}
    assert _pe_id(24450) not in patch_  # target hit does NOT trail the sibling's stop


def test_survivor_at_cost_closes_on_any_premium_rise_above_entry():
    """After a sibling's SL trails this leg's stop to cost (0% loss
    allowed), it must close as soon as its own premium is at or above its
    own entry price — not wait for the full leg_stop_loss_pct threshold."""
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    # PE entry 100, at cost -> SL price is just 100 (not 125). Premium 101 -> hit.
    dhan.quote_data.return_value = _quote_response({_pe_id(24450): 101.0})
    notes = _window_notes()
    notes["leg_state"] = {_ce_id(24450): {"status": "closed", "closed_reason": "leg_sl"}, _pe_id(24450): {"sl_at_cost": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25, "leg_target_pct": 80})

    decision = strategy.evaluate_leg_exits(ctx, notes)

    assert decision is not None
    assert decision["close_security_ids"] == [_pe_id(24450)]


def test_no_leg_exits_when_nothing_open():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    notes = _window_notes()
    notes["leg_state"] = {sid: {"status": "closed"} for leg in notes["legs"] for sid in [str(leg["security_id"])]}
    ctx = StrategyContext(dhan_client=dhan, params={})
    assert strategy.evaluate_leg_exits(ctx, notes) is None
    dhan.quote_data.assert_not_called()


# --- whole-run exit ---


def test_evaluate_exit_true_at_end_time():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45"})
    with _patched_now(datetime.now(IST).replace(hour=14, minute=45, second=0, microsecond=0)):
        assert strategy.evaluate_exit(ctx, _window_notes()) is True


def test_evaluate_exit_true_on_daily_stop_loss():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    legs = _window_notes()["legs"]
    dhan.quote_data.return_value = _quote_response({str(leg["security_id"]): 200.0 for leg in legs})  # heavy loss
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45", "daily_stop_loss": 100, "daily_target": 0})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, _window_notes()) is True


def test_evaluate_exit_false_when_nothing_open():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = MagicMock()
    notes = _window_notes()
    notes["leg_state"] = {sid: {"status": "closed"} for leg in notes["legs"] for sid in [str(leg["security_id"])]}
    ctx = StrategyContext(dhan_client=dhan, params={"end_time": "14:45"})
    with _patched_now(_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is False


# --- T/M/B rolling ---


def test_downward_shift_matches_original_strategy_when_both_legs_still_open():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _window_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(24450), _pe_id(24450)}
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24300}
    assert roll["allow_empty_close"] is True
    assert set(roll["leg_state_patch"]) == {_ce_id(24450), _pe_id(24450)}
    assert all(v == {"in_window": False} for v in roll["leg_state_patch"].values())


def test_roll_at_boundary_with_both_legs_already_leg_exited_still_opens_fresh_pair():
    """The exact scenario this strategy exists for: T's CE hit target and
    PE hit its (cost-trailed) stop well before spot ever reached the T
    boundary — by the time spot does reach it, nothing is left open there,
    but the window still needs a fresh replacement pair."""
    strategy = ThreePairRollingLegSLTargetStrategy()
    # Spot reaching B (24350) shifts the window down, closing the current T (24450).
    dhan = _mock_dhan_client(spot=24350.0)
    notes = _window_notes()
    notes["leg_state"] = {_ce_id(24450): {"status": "closed", "closed_reason": "leg_target"}, _pe_id(24450): {"status": "closed", "closed_reason": "leg_sl"}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == []  # nothing left open at old T to reverse
    assert roll["allow_empty_close"] is True
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24300}  # new B = old B - gap
    assert set(roll["leg_state_patch"]) == {_ce_id(24450), _pe_id(24450)}


def test_roll_at_boundary_with_one_leg_still_open_closes_only_that_one():
    strategy = ThreePairRollingLegSLTargetStrategy()
    dhan = _mock_dhan_client(spot=24350.0)  # spot at B -> shift down, close T
    notes = _window_notes()
    # T's CE already exited on target; PE is still open.
    notes["leg_state"] = {_ce_id(24450): {"status": "closed", "closed_reason": "leg_target"}}
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    roll = decision["rolls"][0]
    assert roll["close_security_ids"] == [_pe_id(24450)]  # only the still-open sibling
    assert roll["allow_empty_close"] is True


def test_rolled_away_strike_does_not_count_toward_window_on_a_later_pass():
    """After a roll, the window (in_window filter) must show exactly the 3
    new strikes — the old rolled-away one must not linger and break the
    "exactly 3" check on a subsequent evaluate_rolls call."""
    strategy = ThreePairRollingLegSLTargetStrategy()
    notes = _window_notes()
    # Simulate the state right after a downward shift: old T (24450) rolled
    # away, new B (24300) opened; B/M unchanged.
    notes["legs"] = notes["legs"] + _strike_legs(24300, "B")
    notes["leg_state"] = {
        _ce_id(24450): {"status": "closed", "in_window": False},
        _pe_id(24450): {"status": "closed", "in_window": False},
    }

    # Now spot sits comfortably inside the new window (24300 < spot < 24400) -> no roll.
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})
    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is None  # spot inside window, nothing to do -- but crucially, no crash/misfire from a 4-strike window
