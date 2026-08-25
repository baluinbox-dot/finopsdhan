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


def _ce_id(strike: int) -> str:
    return str(1000 + strike // 50)


def _pe_id(strike: int) -> str:
    return str(2000 + strike // 50)


@pytest.fixture(autouse=True)
def _patch_lot_size():
    with patch("app.strategies.three_pair_rolling.get_lot_size", return_value=75):
        yield


# --- entry ---


def test_entry_creates_a_t_m_b_window_at_atm_plus_minus_gap():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0)
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 6  # B/M/T x (CE + PE)
    assert {leg.transaction_type for leg in legs} == {"SELL"}


def test_entry_tags_legs_with_the_underlyings_own_derivative_segment():
    """Regression: every leg's exchange_segment must come from the traded
    underlying, not a hardcoded NSE_FNO — SENSEX options trade on BSE_FNO,
    not NSE_FNO. Mistagging this makes live-P&L quote lookups and the
    daily stop-loss/target check silently fail forever for that position
    (both key legs by exchange_segment), even though it looks "Active"."""
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"underlying": "SENSEX", "expiry": "2026-08-27", "strike_gap": 50},
        today_run_count=0,
    )
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert {leg.exchange_segment for leg in legs} == {"BSE_FNO"}

    # NIFTY (and the other NSE indices) must still get NSE_FNO.
    dhan_nifty = _mock_dhan_client(spot=24400.0)
    ctx_nifty = StrategyContext(
        dhan_client=dhan_nifty, params={"expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0
    )
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        nifty_legs = strategy.evaluate_entry(ctx_nifty)
    assert {leg.exchange_segment for leg in nifty_legs} == {"NSE_FNO"}

    def strike_of(leg):
        return int(leg.trading_symbol.split()[1])

    strikes = sorted({strike_of(leg) for leg in legs})
    assert strikes == [24350, 24400, 24450]
    # Each strike has exactly a CE and a PE.
    for strike in strikes:
        sides = {leg.trading_symbol.split()[2] for leg in legs if strike_of(leg) == strike}
        assert sides == {"CE", "PE"}


def test_entry_defaults_to_limit_orders():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50}, today_run_count=0)
    strategy = ThreePairRollingStrategy()
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)
    assert legs is not None
    assert all(leg.order_type == "LIMIT" for leg in legs)


def test_entry_honors_market_order_type_when_configured():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "strike_gap": 50, "order_type": "MARKET"},
        today_run_count=0,
    )
    strategy = ThreePairRollingStrategy()
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)
    assert legs is not None
    assert all(leg.order_type == "MARKET" for leg in legs)


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


# --- entry: hedge ---


def test_entry_with_hedge_adds_one_ce_and_one_pe_sized_for_all_three_pairs():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 1, "hedge_enabled": True, "hedge_premium_target": 5},
        today_run_count=0,
    )
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 8  # 3 pairs x (CE+PE) + 1 CE hedge + 1 PE hedge

    primary = [leg for leg in legs if leg.role == "primary"]
    hedges = [leg for leg in legs if leg.role == "hedge"]
    assert len(primary) == 6
    assert len(hedges) == 2
    assert {leg.transaction_type for leg in hedges} == {"BUY"}
    assert {leg.trading_symbol.split()[2] for leg in hedges} == {"CE", "PE"}

    # 1 lot per pair x 3 pairs = 3x a single pair's lot size on each hedge side.
    for leg in hedges:
        assert leg.quantity == 75 * 1 * 3
    for leg in primary:
        assert leg.quantity == 75 * 1


def test_entry_with_hedge_scales_3x_with_lots_per_pair():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan,
        params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 2, "hedge_enabled": True, "hedge_premium_target": 5},
        today_run_count=0,
    )
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    hedges = [leg for leg in legs if leg.role == "hedge"]
    primary = [leg for leg in legs if leg.role == "primary"]
    for leg in hedges:
        assert leg.quantity == 75 * 2 * 3  # 2 lots/pair x 3 pairs = 6 lots each side
    for leg in primary:
        assert leg.quantity == 75 * 2


def test_entry_hedge_disabled_by_default_adds_no_hedge_legs():
    dhan = _mock_dhan_client(spot=24400.0)
    ctx = StrategyContext(
        dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50, "lots": 1}, today_run_count=0,
    )
    strategy = ThreePairRollingStrategy()

    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 6
    assert all(leg.role == "primary" for leg in legs)


# --- helpers for exit/roll tests ---


def _strike_legs(strike: int, role: str = "T", ce_price: float = 60.0, pe_price: float = 55.0, quantity: int = 75) -> list[dict]:
    return [
        {"label": f"{role} SELL {strike} CE", "security_id": _ce_id(strike), "trading_symbol": f"NIFTY {strike} CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": ce_price, "role": "primary"},
        {"label": f"{role} SELL {strike} PE", "security_id": _pe_id(strike), "trading_symbol": f"NIFTY {strike} PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": pe_price, "role": "primary"},
    ]


def _hedge_legs(ce_strike: int = 24200, pe_strike: int = 24600, quantity: int = 225) -> list[dict]:
    return [
        {"label": f"HEDGE BUY {ce_strike} CE", "security_id": _ce_id(ce_strike), "trading_symbol": f"NIFTY {ce_strike} CE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 5.0, "role": "hedge"},
        {"label": f"HEDGE BUY {pe_strike} PE", "security_id": _pe_id(pe_strike), "trading_symbol": f"NIFTY {pe_strike} PE 2026-08-27",
         "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": quantity, "order_type": "LIMIT",
         "product_type": "INTRADAY", "price": 5.0, "role": "hedge"},
    ]


def _window_notes(b: int = 24350, m: int = 24400, t: int = 24450, hedge: bool = False, **overrides) -> dict:
    legs = _strike_legs(b, "B") + _strike_legs(m, "M") + _strike_legs(t, "T")
    if hedge:
        legs += _hedge_legs()
    notes = {"legs": legs, "entry_premium": 0}
    notes.update(overrides)
    return notes


# --- rolling: downward shift ---


def test_downward_shift_closes_top_opens_new_bottom():
    strategy = ThreePairRollingStrategy()
    # Window B=24350 M=24400 T=24450; spot reaches B (24350) -> shift down.
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _window_notes())

    assert decision is not None
    assert len(decision["rolls"]) == 1
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(24450), _pe_id(24450)}  # old T closed
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24300}  # new B = old B - gap


def test_roll_honors_market_order_type_when_configured():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50, "order_type": "MARKET"})

    decision = strategy.evaluate_rolls(ctx, _window_notes())

    assert decision is not None
    roll = decision["rolls"][0]
    assert all(leg.order_type == "MARKET" for leg in roll["new_legs"])


def test_upward_shift_closes_bottom_opens_new_top():
    strategy = ThreePairRollingStrategy()
    # Window B=24350 M=24400 T=24450; spot reaches T (24450) -> shift up.
    dhan = _mock_dhan_client(spot=24450.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _window_notes())

    assert decision is not None
    assert len(decision["rolls"]) == 1
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(24350), _pe_id(24350)}  # old B closed
    new_strikes = {int(leg.trading_symbol.split()[1]) for leg in roll["new_legs"]}
    assert new_strikes == {24500}  # new T = old T + gap


def test_downward_shift_never_touches_hedge_legs():
    """The hedge is bought once at entry and never rolls with T/M/B --
    a shift must only ever name primary-role legs in close_security_ids
    or new_legs, regardless of where the hedge strikes happen to sit."""
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _window_notes(hedge=True))

    assert decision is not None
    roll = decision["rolls"][0]
    hedge_sids = {leg["security_id"] for leg in _hedge_legs()}
    assert not (set(roll["close_security_ids"]) & hedge_sids)
    assert all(leg.role == "primary" for leg in roll["new_legs"])


def test_roll_still_works_when_a_hedge_strike_collides_with_a_window_strike():
    """A hedge CE/PE strike landing on the exact same strike as one of the
    open T/M/B legs must not get swept into that strike's group -- the
    window must still be read as exactly 3 primary strikes."""
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    # Hedge CE strike deliberately collides with the open Top (24450).
    legs = _strike_legs(24350, "B") + _strike_legs(24400, "M") + _strike_legs(24450, "T") + _hedge_legs(ce_strike=24450)
    notes = {"legs": legs, "entry_premium": 0}

    decision = strategy.evaluate_rolls(ctx, notes)

    assert decision is not None
    assert len(decision["rolls"]) == 1
    roll = decision["rolls"][0]
    # Exactly the primary T pair, nothing more -- the hedge's security_id
    # happens to equal the primary CE's here (same real contract), so this
    # checks list length too, not just set membership, to catch the hedge
    # sneaking in as a spurious 3rd close_security_id.
    assert len(roll["close_security_ids"]) == 2
    assert set(roll["close_security_ids"]) == {_ce_id(24450), _pe_id(24450)}
    # New legs sized off the primary's own quantity (75), not accidentally
    # off the hedge's 225 -- would only happen if the hedge leaked in.
    assert {leg.quantity for leg in roll["new_legs"]} == {75}


def test_no_shift_when_spot_inside_window():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24400.0)  # exactly M, strictly inside (B, T)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})
    assert strategy.evaluate_rolls(ctx, _window_notes()) is None


def test_matches_the_spec_worked_example_continuous_downward_rolling():
    """From the requirements doc: B=24350,M=24400,T=24450 -> spot 24350 ->
    T=24400,M=24350,B=24300 -> spot 24300 -> T=24350,M=24300,B=24250."""
    strategy = ThreePairRollingStrategy()

    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})
    decision = strategy.evaluate_rolls(ctx, _window_notes(b=24350, m=24400, t=24450))
    assert decision is not None
    roll = decision["rolls"][0]
    assert {int(l.trading_symbol.split()[1]) for l in roll["new_legs"]} == {24300}

    # Apply the shift by hand to build the next state: B=24300,M=24350,T=24400.
    dhan2 = _mock_dhan_client(spot=24300.0)
    ctx2 = StrategyContext(dhan_client=dhan2, params={"expiry": "2026-08-27", "strike_gap": 50})
    decision2 = strategy.evaluate_rolls(ctx2, _window_notes(b=24300, m=24350, t=24400))
    assert decision2 is not None
    roll2 = decision2["rolls"][0]
    assert set(roll2["close_security_ids"]) == {_ce_id(24400), _pe_id(24400)}
    assert {int(l.trading_symbol.split()[1]) for l in roll2["new_legs"]} == {24250}


def test_reversal_can_reopen_a_previously_used_strike():
    """From the spec's complete-flow example: after two downward shifts
    (window now B=24250,M=24300,T=24350), spot reverses back up to 24350
    (= current T) -> close B(24250), open new T=24400 -- even though
    24400 was part of the window earlier today and has since been closed.
    There is no "different pair" identity to block this in the T-M-B
    model; the shift is legitimate."""
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    notes = _window_notes(b=24250, m=24300, t=24350)
    # Leg history also contains the earlier (now-closed) 24450 and 24400 pairs.
    historical = _strike_legs(24450, "T") + _strike_legs(24400, "T")
    notes["legs"] = notes["legs"] + historical
    notes["leg_state"] = {leg["security_id"]: {"status": "closed"} for leg in historical}

    decision = strategy.evaluate_rolls(ctx, notes)
    assert decision is not None
    roll = decision["rolls"][0]
    assert set(roll["close_security_ids"]) == {_ce_id(24250), _pe_id(24250)}
    assert {int(l.trading_symbol.split()[1]) for l in roll["new_legs"]} == {24400}


def test_big_gap_produces_multiple_sequential_shifts_without_collision():
    strategy = ThreePairRollingStrategy()
    # Window B=24350 M=24400 T=24450; spot gaps down to 24275 in a single
    # poll -> two downward shifts (24275 clears the original B(24350) and
    # the next B(24300), but not the B after that (24250), so exactly 2).
    dhan = _mock_dhan_client(spot=24275.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    decision = strategy.evaluate_rolls(ctx, _window_notes())

    assert decision is not None
    assert len(decision["rolls"]) == 2

    # First shift: close T(24450), open new B(24300).
    assert set(decision["rolls"][0]["close_security_ids"]) == {_ce_id(24450), _pe_id(24450)}
    assert {int(l.trading_symbol.split()[1]) for l in decision["rolls"][0]["new_legs"]} == {24300}

    # Second shift computed off the *updated* window (B=24300,M=24350,T=24400):
    # spot 24250 still <= new B(24300) -> close new T(24400), open new B(24250).
    assert set(decision["rolls"][1]["close_security_ids"]) == {_ce_id(24400), _pe_id(24400)}
    assert {int(l.trading_symbol.split()[1]) for l in decision["rolls"][1]["new_legs"]} == {24250}

    # The two shifts never target the same strike.
    targets = [{int(l.trading_symbol.split()[1]) for l in r["new_legs"]} for r in decision["rolls"]]
    assert targets[0] != targets[1]


def test_no_roll_when_window_is_not_exactly_three_strikes():
    strategy = ThreePairRollingStrategy()
    dhan = _mock_dhan_client(spot=24350.0)
    ctx = StrategyContext(dhan_client=dhan, params={"expiry": "2026-08-27", "strike_gap": 50})

    notes = _window_notes()
    # Mark the T leg's already closed (e.g. daily SL was about to finish the run) -- malformed window, don't touch.
    t_ids = {leg["security_id"] for leg in _strike_legs(24450, "T")}
    notes["leg_state"] = {sid: {"status": "closed"} for sid in t_ids}

    assert strategy.evaluate_rolls(ctx, notes) is None


# --- daily stop-loss / target / end time ---


def test_exit_forced_at_end_time():
    strategy = ThreePairRollingStrategy()
    ctx = StrategyContext(dhan_client=MagicMock(), params={"end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time().replace(hour=15)):
        assert strategy.evaluate_exit(ctx, _window_notes()) is True


def test_exit_daily_stop_loss_triggers_on_live_unrealized_pnl():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    quotes = {}
    for strike in (24350, 24400, 24450):
        quotes[_ce_id(strike)] = {"last_price": 200.0}
        quotes[_pe_id(strike)] = {"last_price": 200.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 10000, "daily_target": 0, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _window_notes()) is True


def test_exit_daily_target_triggers_including_realized_so_far():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    quotes = {}
    for strike in (24350, 24400, 24450):
        quotes[_ce_id(strike)] = {"last_price": 5.0}
        quotes[_pe_id(strike)] = {"last_price": 5.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    notes = _window_notes(realized_pnl_so_far=5000)
    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 0, "daily_target": 15000, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, notes) is True


def test_exit_holds_when_within_bounds():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    quotes = {}
    for strike in (24350, 24400, 24450):
        quotes[_ce_id(strike)] = {"last_price": 58.0}
        quotes[_pe_id(strike)] = {"last_price": 53.0}
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": quotes}}}

    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 10000, "daily_target": 15000, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _window_notes()) is False


def test_exit_missing_quote_holds_rather_than_guessing():
    strategy = ThreePairRollingStrategy()
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {}}}}
    ctx = StrategyContext(dhan_client=dhan, params={"daily_stop_loss": 1, "daily_target": 1, "end_time": "14:45"})
    with patch("app.strategies.three_pair_rolling._now_ist", return_value=_within_window_time()):
        assert strategy.evaluate_exit(ctx, _window_notes()) is False
