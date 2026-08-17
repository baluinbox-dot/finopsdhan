from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.single_leg_seller_hedge import SingleLegSellerWithHedge

IST = ZoneInfo("Asia/Kolkata")

# Strikes: 23600 23800 24000(ATM) 24200 24400 — realistic shape: CE premium
# falls as strike rises above spot, PE premium falls as strike drops below spot.
#
# Dhan's real API double-wraps option-chain/expiry-list payloads: the SDK's
# own {"status", "data"} envelope contains *another* {"status", "data"}
# envelope inside it — verified live against the real API. Mocks match that
# actual shape so these tests don't silently drift from reality.
CHAIN_RESPONSE = {
    "status": "success",
    "data": {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                "23600.000000": {"pe": {"security_id": 11, "last_price": 3.0, "greeks": {}}, "ce": {"security_id": 61, "last_price": 150.0, "greeks": {}}},
                "23800.000000": {"pe": {"security_id": 21, "last_price": 8.0, "greeks": {}}, "ce": {"security_id": 71, "last_price": 90.0, "greeks": {}}},
                "24000.000000": {"pe": {"security_id": 31, "last_price": 50.0, "greeks": {}}, "ce": {"security_id": 81, "last_price": 50.0, "greeks": {}}},
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
    # A fixed instant safely inside the default 09:15-15:15 IST window.
    now = datetime.now(IST)
    return now.replace(hour=11, minute=0, second=0, microsecond=0)


def _outside_window_time():
    now = datetime.now(IST)
    return now.replace(hour=16, minute=0, second=0, microsecond=0)


def _sl(**conditions) -> dict:
    """Build a stop_loss/target dict, e.g. _sl(premium_pct=(True, 30), spot_level=(True, 24500))."""
    base = {"premium_pct": {"enabled": False, "value": 0}, "premium_abs": {"enabled": False, "value": 0}, "spot_level": {"enabled": False, "value": 0}}
    for key, (enabled, value) in conditions.items():
        base[key] = {"enabled": enabled, "value": value}
    return base


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.single_leg_seller_hedge.get_lot_size", return_value=75):
        yield


def test_entry_sells_1st_otm_ce():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"option_type": "CE", "otm_level": 1, "expiry": "2026-08-27", "lots": 1},
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 1
    leg = legs[0]
    assert leg.transaction_type == "SELL"
    assert leg.security_id == "91"  # CE at 24200, 1 strike above ATM (24000)
    assert leg.role == "primary"
    assert leg.quantity == 75


def test_entry_sells_2nd_otm_pe():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"option_type": "PE", "otm_level": 2, "expiry": "2026-08-27", "lots": 1},
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    leg = legs[0]
    assert leg.transaction_type == "SELL"
    assert leg.security_id == "11"  # PE at 23600, 2 strikes below ATM (24000)


def test_entry_by_closest_premium():
    dhan = _mock_dhan_client()
    # CE side, target premium ~8 -> should pick strike 24200 (ce_ltp exactly 8.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "strike_selection_mode": "premium_closest",
            "strike_premium_target": 8.0,
            "expiry": "2026-08-27",
            "lots": 1,
        },
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert legs[0].security_id == "91"  # CE at 24200, premium 8.0 matches exactly


def test_entry_by_closest_premium_can_pick_atm():
    dhan = _mock_dhan_client()
    # CE side, target premium ~50 -> ATM (24000) itself is the closest match
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "strike_selection_mode": "premium_closest",
            "strike_premium_target": 50.0,
            "expiry": "2026-08-27",
            "lots": 1,
        },
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert legs[0].security_id == "81"  # ATM CE, premium 50.0


def test_entry_blocked_outside_trading_window():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"option_type": "CE", "otm_level": 1, "expiry": "2026-08-27"})
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_outside_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"option_type": "CE", "otm_level": 1, "expiry": "2026-08-27"},
        today_run_count=1,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_with_hedge_picks_nearest_premium():
    dhan = _mock_dhan_client()
    # Sell 1st OTM CE (24200, premium 8), hedge target premium ~3 -> should pick 24400 CE (premium 3.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "otm_level": 1,
            "expiry": "2026-08-27",
            "hedge_enabled": True,
            "hedge_premium_target": 3.0,
        },
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 2
    hedge_leg = [leg for leg in legs if leg.role == "hedge"][0]
    assert hedge_leg.transaction_type == "BUY"
    assert hedge_leg.security_id == "101"  # CE at 24400


def test_entry_aborts_when_hedge_requested_but_unavailable():
    dhan = _mock_dhan_client()
    # OTM level 3 on CE side is off the edge of our mocked chain (only 2 strikes above ATM),
    # so there's nothing further out to hedge with even if entry itself were otherwise valid.
    # Use otm_level=2 (24400, last strike) so there is no strike beyond it to hedge with.
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "otm_level": 2,
            "expiry": "2026-08-27",
            "hedge_enabled": True,
            "hedge_premium_target": 1.0,
        },
        today_run_count=0,
    )
    strategy = SingleLegSellerWithHedge()

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_exit_forced_at_window_end():
    strategy = SingleLegSellerWithHedge()
    ctx = StrategyContext(dhan_client=MagicMock(), params={"window_end": "15:15"})
    open_run_notes = {"legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}], "entry_premium": 90.0}

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_outside_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_premium_pct_stop_loss():
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"90": {"last_price": 130.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "stop_loss": _sl(premium_pct=(True, 30)),
            "target": _sl(premium_pct=(True, 50)),
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    # 90 -> 130 is a ~44% adverse move, past the 30% stop-loss
    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_premium_abs_stop_loss():
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"90": {"last_price": 130.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            # premium_pct disabled entirely, only absolute premium SL enabled
            "stop_loss": _sl(premium_abs=(True, 120)),
            "target": _sl(),
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_premium_abs_target():
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"90": {"last_price": 20.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "stop_loss": _sl(),
            "target": _sl(premium_abs=(True, 25)),  # exit once premium falls to 25 or below
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_multiple_conditions_whichever_hits_first():
    """Both premium_pct and spot_level SL enabled; premium hasn't moved enough
    but spot has breached the level — should still exit."""
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"90": {"last_price": 92.0}}}}}
    dhan.ticker_data.return_value = {"status": "success", "data": {"status": "success", "data": {"IDX_I": {"13": {"last_price": 24600.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "underlying": "NIFTY",
            "option_type": "CE",
            "stop_loss": _sl(premium_pct=(True, 30), spot_level=(True, 24500)),  # spot breach at 24500
            "target": _sl(premium_pct=(True, 50)),
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    # premium only moved 90->92 (~2%, well under 30% SL) but spot 24600 >= 24500 level -> exit
    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_spot_level_target_for_pe_seller():
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.ticker_data.return_value = {"status": "success", "data": {"status": "success", "data": {"IDX_I": {"13": {"last_price": 24300.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "underlying": "NIFTY",
            "option_type": "PE",
            "stop_loss": _sl(spot_level=(True, 23500)),
            "target": _sl(spot_level=(True, 24250)),
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "40", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 8.0,
    }

    # PE seller profits as spot rises; 24300 >= target 24250 -> target hit
    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_exit_holds_when_nothing_triggered():
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"90": {"last_price": 92.0}}}}}
    ctx = StrategyContext(
        dhan_client=dhan,
        params={
            "option_type": "CE",
            "stop_loss": _sl(premium_pct=(True, 30)),
            "target": _sl(premium_pct=(True, 50)),
        },
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is False


def test_exit_holds_when_no_conditions_enabled():
    """Nothing checked at all -> never auto-exits (still exits on window end or manual close)."""
    strategy = SingleLegSellerWithHedge()
    dhan = MagicMock()
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"option_type": "CE", "stop_loss": _sl(), "target": _sl()},
    )
    open_run_notes = {
        "legs": [{"security_id": "90", "exchange_segment": "NSE_FNO", "transaction_type": "SELL"}],
        "entry_premium": 90.0,
    }

    with patch("app.strategies.single_leg_seller_hedge._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, open_run_notes) is False
        dhan.quote_data.assert_not_called()
        dhan.ticker_data.assert_not_called()
