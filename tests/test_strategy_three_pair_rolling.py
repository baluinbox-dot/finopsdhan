from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import StrategyContext
from app.strategies.three_pair_rolling import ThreePairRollingStrategy

IST = ZoneInfo("Asia/Kolkata")

# Strikes every 50 points from 24100 to 24700, spot = 24400.
# security_id scheme: ce = 1000 + strike/50, pe = 2000 + strike/50 (unique, easy to eyeball).
_STRIKES = list(range(24100, 24701, 50))


def _row(strike: int) -> dict:
    idx = strike // 50
    return {
        "ce_security_id": 1000 + idx, "ce_ltp": 60.0, "pe_security_id": 2000 + idx, "pe_ltp": 55.0,
    }


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
    now = datetime.now(IST)
    return now.replace(hour=11, minute=0, second=0, microsecond=0)


def _before_start_time():
    now = datetime.now(IST)
    return now.replace(hour=9, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.three_pair_rolling.get_lot_size", return_value=75):
        yield


# --- entry ---


def test_entry_creates_three_pairs_at_atm_plus_minus_gap():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0)
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 6  # 3 pairs x (CE + PE)
    assert {leg.transaction_type for leg in legs} == {"SELL"}

    by_pair: dict[str, list] = {}
    for leg in legs:
        by_pair.setdefault(leg.pair_id, []).append(leg)

    assert set(by_pair) == {"FIN1", "FIN2", "FIN3"}
    assert all(len(v) == 2 for v in by_pair.values())

    def strike_of(leg):
        return int(leg.trading_symbol.split()[1])

    assert {strike_of(leg) for leg in by_pair["FIN1"]} == {24450}
    assert {strike_of(leg) for leg in by_pair["FIN2"]} == {24400}
    assert {strike_of(leg) for leg in by_pair["FIN3"]} == {24350}


def test_entry_blocked_before_start_time():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=0)
    strategy = ThreePairRollingStrategy()
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_before_start_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_blocked_after_one_entry_today():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27"}, today_run_count=1)
    strategy = ThreePairRollingStrategy()
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


def test_entry_requires_expiry():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": ""}, today_run_count=0)
    strategy = ThreePairRollingStrategy()
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_entry(ctx) is None


# --- helpers for exit/roll tests ---


def _pair_legs(pair_id: str, strike: int, ce_price: float = 60.0, pe_price: float = 55.0, quantity: int = 75) -> list[dict]:
    idx = strike // 50
    return [
        {"label": f"{pair_id} SELL {strike} CE", "security_id": str(1000 + idx), "trading_symbol": f"NIFTY {strike} CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": ce_price, "role": "primary", "pair_id": pair_id},
        {"label": f"{pair_id} SELL {strike} PE", "security_id": str(2000 + idx), "trading_symbol": f"NIFTY {strike} PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": pe_price, "role": "primary", "pair_id": pair_id},
    ]


def _three_pair_notes(**overrides) -> dict:
    legs = _pair_legs("FIN1", 24450) + _pair_legs("FIN2", 24400) + _pair_legs("FIN3", 24350)
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


# --- rolling ---


def test_rolls_pair_down_when_spot_moves_two_gaps_below_its_strike():
    strategy = ThreePairRollingStrategy()
    # FIN1 at 24450; spot 24350 = 24450 - 2*50 -> should roll to nearest(24350-50)=24300.
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _three_pair_notes())

    assert decision is not None
    assert len(decision["rolls"]) == 1
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {"1489", "2489"}  # FIN1's 24450 legs (idx=489)
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24300}
    assert {leg.pair_id for leg in roll["new_legs"]} == {"FIN1"}


def test_rolls_pair_up_when_spot_moves_two_gaps_above_its_strike():
    strategy = ThreePairRollingStrategy()
    # FIN3 at 24350; spot 24450 = 24350 + 2*50 -> should roll to nearest(24450+50)=24500.
    dhan = _mock_dhan_client(spot=24450.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _three_pair_notes())

    assert decision is not None
    assert len(decision["rolls"]) == 1
    roll = decision["rolls"][0]
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24500}
    assert {leg.pair_id for leg in roll["new_legs"]} == {"FIN3"}


def test_no_roll_when_spot_within_two_gaps_of_every_pair():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24400.0)  # unchanged from entry
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})
    assert strategy.evaluate_rolls(ctx, _three_pair_notes()) is None


def test_roll_blocked_when_target_strike_already_owned_by_another_pair():
    strategy = ThreePairRollingStrategy()
    # FIN1 due to roll to 24300 (spot 24350), but FIN3 already held 24300 earlier today
    # (recorded in leg history even though FIN3 has since moved elsewhere / is still open at 24350).
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    notes = _three_pair_notes()
    # Append a historical (already-closed) FIN3 leg pair at 24300, owned by FIN3.
    historical = _pair_legs("FIN3", 24300)
    notes["legs"] = notes["legs"] + historical
    notes["leg_state"] = {leg["security_id"]: {"status": "closed"} for leg in historical}

    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is None  # FIN1's roll to 24300 is blocked; nothing else due this pass


def test_roll_allowed_when_pair_revisits_its_own_past_strike():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    notes = _three_pair_notes()
    # FIN1 itself previously held 24300 earlier today -> revisiting it now is fine.
    historical = _pair_legs("FIN1", 24300)
    notes["legs"] = notes["legs"] + historical
    notes["leg_state"] = {leg["security_id"]: {"status": "closed"} for leg in historical}

    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is not None
    assert len(decision["rolls"]) == 1


def test_no_roll_for_pair_already_closed():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    notes = _three_pair_notes()
    fin1_ids = {leg["security_id"] for leg in _pair_legs("FIN1", 24450)}
    notes["leg_state"] = {sid: {"status": "closed"} for sid in fin1_ids}

    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is None  # FIN1 already fully closed (e.g. daily SL about to finish the run) -- nothing to roll


# --- daily stop-loss / target / end time ---


def test_exit_forced_at_end_time():
    strategy = ThreePairRollingStrategy()
    ctx = StrategyContext(dhan_client=MagicMock(), params={"end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time().replace(hour=15)):
        assert strategy.evaluate_exit(ctx, _three_pair_notes()) is True


def test_exit_daily_stop_loss_triggers_on_live_unrealized_pnl():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    # All 6 legs (entry ~60/55 each) now priced way higher -> big unrealized loss.
    quotes = {}
    for pair, strike in (("FIN1", 24450), ("FIN2", 24400), ("FIN3", 24350)):
        idx = strike // 50
        quotes[str(1000 + idx)] = {"last_price": 200.0}
        quotes[str(2000 + idx)] = {"last_price": 200.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 10000, "daily_target": 0, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _three_pair_notes()) is True


def test_exit_daily_target_triggers_including_realized_so_far():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    quotes = {}
    for pair, strike in (("FIN1", 24450), ("FIN2", 24400), ("FIN3", 24350)):
        idx = strike // 50
        quotes[str(1000 + idx)] = {"last_price": 5.0}
        quotes[str(2000 + idx)] = {"last_price": 5.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    # Combined entry ~115 * 3 pairs * 75 qty ~ already large unrealized profit even
    # without prior realized P&L, but also check realized_pnl_so_far is additive.
    notes = _three_pair_notes(realized_pnl_so_far=5000)
    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 0, "daily_target": 15000, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


def test_exit_holds_when_within_bounds():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    quotes = {}
    for pair, strike in (("FIN1", 24450), ("FIN2", 24400), ("FIN3", 24350)):
        idx = strike // 50
        quotes[str(1000 + idx)] = {"last_price": 58.0}
        quotes[str(2000 + idx)] = {"last_price": 53.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 10000, "daily_target": 15000, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _three_pair_notes()) is False


def test_exit_missing_quote_holds_rather_than_guessing():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {}}}}
    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 1, "daily_target": 1, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _three_pair_notes()) is False
