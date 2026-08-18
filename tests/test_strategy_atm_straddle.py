from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.atm_straddle_trigger_hedge import ATMStraddleTriggerHedge
from app.strategies.base import StrategyContext

IST = ZoneInfo("Asia/Kolkata")

# Strikes: 23600 23800 24000(ATM) 24200 24400 — same shape as the
# single-leg-hedge fixture: CE premium falls as strike rises above spot,
# PE premium falls as strike drops below spot.
CHAIN_RESPONSE = {
    "status": "success",
    "data": {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                "23600.000000": {"pe": {"security_id": 11, "last_price": 5.0, "greeks": {}}, "ce": {"security_id": 61, "last_price": 150.0, "greeks": {}}},
                "23800.000000": {"pe": {"security_id": 21, "last_price": 10.0, "greeks": {}}, "ce": {"security_id": 71, "last_price": 90.0, "greeks": {}}},
                "24000.000000": {"pe": {"security_id": 31, "last_price": 55.0, "greeks": {}}, "ce": {"security_id": 81, "last_price": 60.0, "greeks": {}}},
                "24200.000000": {"pe": {"security_id": 41, "last_price": 90.0, "greeks": {}}, "ce": {"security_id": 91, "last_price": 8.0, "greeks": {}}},
                "24400.000000": {"pe": {"security_id": 51, "last_price": 150.0, "greeks": {}}, "ce": {"security_id": 101, "last_price": 3.0, "greeks": {}}},
            },
        },
    },
}


def _mock_dhan_client() -> MagicMock:
    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": ["2026-08-27"]}}
    dhan.option_chain.return_value = CHAIN_RESPONSE
    return dhan


def _within_window_time():
    now = datetime.now(IST)
    return now.replace(hour=11, minute=0, second=0, microsecond=0)


def _outside_window_time():
    now = datetime.now(IST)
    return now.replace(hour=16, minute=0, second=0, microsecond=0)


def _before_entry_time():
    now = datetime.now(IST)
    return now.replace(hour=9, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.atm_straddle_trigger_hedge.get_lot_size", return_value=75):
        yield


# --- entry ---


def test_entry_blocked_when_premium_hasnt_risen_enough():
    # ATM combined = 60 + 55 = 115. Reference 110, 10% trigger -> needs 121.
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "reference_premium": 110, "entry_trigger_pct": 10, "hedge_enabled": False},
        today_run_count=0,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_sells_atm_straddle_once_pct_trigger_hit():
    # Combined 115 >= 110 * 1.05 = 115.5? No -> use pct=4 so trigger 114.4, 115 clears it.
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "reference_premium": 110, "entry_trigger_pct": 4, "hedge_enabled": False},
        today_run_count=0,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 2
    assert {leg.transaction_type for leg in legs} == {"SELL"}
    assert {leg.security_id for leg in legs} == {"81", "31"}  # ATM CE/PE at 24000
    for leg in legs:
        assert leg.quantity == 75
        assert leg.role == "primary"


def test_entry_flat_trigger_mode():
    # Flat mode: trigger = reference + flat. reference=100, flat=15 -> 115, combined is exactly 115.
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "expiry": "2026-08-27",
            "reference_premium": 100,
            "entry_trigger_mode": "flat",
            "entry_trigger_flat": 15,
            "hedge_enabled": False,
        },
        today_run_count=0,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)
    assert legs is not None


def test_entry_blocked_without_reference_premium():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "reference_premium": 0}, today_run_count=0)
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_before_entry_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "reference_premium": 1, "entry_time": "09:15"},
        today_run_count=0,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_before_entry_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan, params={"expiry": "2026-08-27", "reference_premium": 1}, today_run_count=1,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_with_hedge_adds_both_sides():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "expiry": "2026-08-27",
            "reference_premium": 110,
            "entry_trigger_pct": 4,
            "hedge_enabled": True,
            "hedge_premium_target": 8.0,  # nearest to 8.0 -> CE 24200 (8.0 exact), PE 23800 (10.0, closest available)
        },
        today_run_count=0,
    )
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 4
    hedges = [leg for leg in legs if leg.role == "hedge"]
    assert len(hedges) == 2
    assert {leg.transaction_type for leg in hedges} == {"BUY"}
    assert {leg.security_id for leg in hedges} == {"91", "21"}  # CE 24200 hedge, PE 23800 hedge


def test_entry_aborts_when_hedge_unavailable():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "expiry": "2026-08-27",
            "reference_premium": 1,
            "entry_trigger_pct": 0,
            "hedge_enabled": True,
            "hedge_premium_target": 1.0,
        },
        today_run_count=0,
    )
    # Use a chain with only the ATM strike so there's nothing further OTM to hedge with.
    dhan.option_chain.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"last_price": 24000.0, "oc": {
            "24000.000000": {"pe": {"security_id": 31, "last_price": 55.0, "greeks": {}}, "ce": {"security_id": 81, "last_price": 60.0, "greeks": {}}},
        }}},
    }
    strategy = ATMStraddleTriggerHedge()
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- whole-position exit (close time / target) ---


def _open_run_notes(*, ce_price=60.0, pe_price=55.0, leg_state=None, hedge=False):
    legs = [
        {"label": "SELL ATM 24000 CE", "security_id": "81", "trading_symbol": "NIFTY 24000 CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": ce_price, "role": "primary"},
        {"label": "SELL ATM 24000 PE", "security_id": "31", "trading_symbol": "NIFTY 24000 PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": pe_price, "role": "primary"},
    ]
    if hedge:
        legs.append({"label": "HEDGE BUY 24200 CE", "security_id": "91", "trading_symbol": "NIFTY 24200 CE 2026-08-27",
                     "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 75, "order_type": "LIMIT",
                     "product_type": "INTRADAY", "price": 8.0, "role": "hedge"})
        legs.append({"label": "HEDGE BUY 24200 PE", "security_id": "41", "trading_symbol": "NIFTY 24200 PE 2026-08-27",
                     "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 75, "order_type": "LIMIT",
                     "product_type": "INTRADAY", "price": 8.0, "role": "hedge"})
    notes = {"legs": legs}
    if leg_state is not None:
        notes["leg_state"] = leg_state
    return notes


def test_exit_forced_at_close_time():
    strategy = ATMStraddleTriggerHedge()
    ctx = StrategyContext(dhan_client=MagicMock(), params={"close_time": "15:15"})
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_outside_window_time()):
        assert strategy.evaluate_exit(ctx, _open_run_notes()) is True


def test_exit_target_hit_on_combined_premium():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    # Entry combined 60+55=115. 80% target -> exit when combined <= 23.
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 10.0}, "31": {"last_price": 9.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"target_pct": 80})
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _open_run_notes()) is True


def test_exit_holds_when_target_not_reached():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 55.0}, "31": {"last_price": 50.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"target_pct": 80})
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _open_run_notes()) is False


def test_exit_target_uses_only_remaining_open_leg_after_one_sl_out():
    """CE already closed via per-leg SL; target should evaluate off the PE
    leg's live premium alone (and not need a CE quote)."""
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"31": {"last_price": 5.0}}}},
    }
    leg_state = {"81": {"status": "closed"}}
    ctx = StrategyContext(dhan_client=dhan, params={"target_pct": 80})
    with patch("app.strategies.atm_straddle_trigger_hedge._now_ist", return_value=_within_window_time()):
        # entry_combined = 60+55=115, target triggers at <=23; PE alone at 5.0 clears it.
        assert strategy.evaluate_exit(ctx, _open_run_notes(leg_state=leg_state)) is True


# --- per-leg stop-loss + trailing ---


def test_leg_exit_closes_only_hit_leg_and_trails_survivor():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    # CE entry 60 -> 25% SL at 75. Current CE 80 (hit), PE entry 55 -> current 56 (not hit).
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 80.0}, "31": {"last_price": 56.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25})
    decision = strategy.evaluate_leg_exits(ctx, _open_run_notes())

    assert decision is not None
    assert decision["close_security_ids"] == ["81"]
    assert decision["leg_state_patch"] == {"31": {"sl_moved_to_cost": True}}


def test_leg_exit_also_closes_matching_hedge():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 80.0}, "31": {"last_price": 56.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25})
    decision = strategy.evaluate_leg_exits(ctx, _open_run_notes(hedge=True))

    assert decision is not None
    assert set(decision["close_security_ids"]) == {"81", "91"}  # CE sell + CE hedge, not the PE hedge


def test_leg_exit_trailed_survivor_stops_at_cost():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    # PE already SL'd out; CE survivor trailed to cost (60). Current CE 61 -> breakeven-stop hit.
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 61.0}}}},
    }
    leg_state = {"31": {"status": "closed"}, "81": {"status": "open", "sl_moved_to_cost": True}}
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25})
    decision = strategy.evaluate_leg_exits(ctx, _open_run_notes(leg_state=leg_state))

    assert decision is not None
    assert decision["close_security_ids"] == ["81"]
    assert decision["leg_state_patch"] == {}  # nothing left to trail


def test_leg_exit_holds_when_nothing_hit():
    strategy = ATMStraddleTriggerHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"81": {"last_price": 62.0}, "31": {"last_price": 56.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"leg_stop_loss_pct": 25})
    assert strategy.evaluate_leg_exits(ctx, _open_run_notes()) is None


def test_leg_exit_noop_when_no_open_primary_legs():
    strategy = ATMStraddleTriggerHedge()
    ctx = StrategyContext(dhan_client=MagicMock(), params={})
    leg_state = {"81": {"status": "closed"}, "31": {"status": "closed"}}
    assert strategy.evaluate_leg_exits(ctx, _open_run_notes(leg_state=leg_state)) is None
