"""End-to-end smoke test for the backtest engine: run DynamicStrangleStrategy
(unmodified -- the actual live/paper strategy class) against a small
synthetic one-day dataset, and confirm it enters, holds, and force-closes
at end_time with costs correctly applied -- proving the whole
data-source -> monkeypatch -> orchestration pipeline works before trusting
it against the real 244MB downloaded dataset."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.backtest.costs import CostModel
from app.backtest.data_source import HistoricalDataSource
from app.backtest.engine import _Runner, run_backtest
from app.strategies.base import OrderLeg
from app.strategies.dynamic_strangle import DynamicStrangleStrategy

IST = ZoneInfo("Asia/Kolkata")
_DAY = date(2024, 9, 4)


def _day_timestamps(hh_start=9, mm_start=15, hh_end=15, mm_end=30, step_min=5) -> list[int]:
    start = datetime(_DAY.year, _DAY.month, _DAY.day, hh_start, mm_start, tzinfo=IST)
    end = datetime(_DAY.year, _DAY.month, _DAY.day, hh_end, mm_end, tzinfo=IST)
    out = []
    t = start
    while t <= end:
        out.append(int(t.timestamp()))
        t = datetime.fromtimestamp(t.timestamp() + step_min * 60, tz=IST)
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "backtest"
    ts_list = _day_timestamps()

    _write_csv(root / "NIFTY" / "spot_5min" / "chunk.csv", [
        {"timestamp": ts, "open": 25000, "high": 25000, "low": 25000, "close": 25000.0, "volume": 0} for ts in ts_list
    ])
    # base_distance=500, 50pt interval -> CE at ATM+10 (25500), PE at ATM-10 (24500).
    # Every strategy in this app requires BOTH sides present at a strike
    # before trusting it (see dynamic_strangle.py's _row helper) -- same
    # defensive check real Dhan chain data would need to satisfy, so both
    # a CE and a PE series are needed at *each* of the two strikes, not
    # just whichever side is actually sold there.
    _write_csv(root / "NIFTY" / "options" / "CE" / "ATM+10" / "chunk.csv", [
        {"timestamp": ts, "open": 60, "high": 60, "low": 60, "close": 60.0, "oi": 100, "spot": 25000.0} for ts in ts_list
    ])
    _write_csv(root / "NIFTY" / "options" / "PE" / "ATM+10" / "chunk.csv", [
        {"timestamp": ts, "open": 5, "high": 5, "low": 5, "close": 5.0, "oi": 100, "spot": 25000.0} for ts in ts_list
    ])
    _write_csv(root / "NIFTY" / "options" / "CE" / "ATM-10" / "chunk.csv", [
        {"timestamp": ts, "open": 5, "high": 5, "low": 5, "close": 5.0, "oi": 100, "spot": 25000.0} for ts in ts_list
    ])
    _write_csv(root / "NIFTY" / "options" / "PE" / "ATM-10" / "chunk.csv", [
        {"timestamp": ts, "open": 55, "high": 55, "low": 55, "close": 55.0, "oi": 100, "spot": 25000.0} for ts in ts_list
    ])
    return root


def test_dynamic_strangle_enters_holds_and_closes_at_end_time(data_root: Path):
    cost_model = CostModel()
    params = {
        # A non-empty placeholder is required -- dynamic_strangle.py's own
        # evaluate_entry guards on `expiry` being truthy before it'll even
        # look at the chain, even though the backtest data source itself
        # never uses the expiry value for anything (a synthetic chain
        # snapshot is purely a function of the simulated timestamp).
        "underlying": "NIFTY", "expiry": "backtest", "lots": 1, "start_time": "09:20", "end_time": "14:45",
        "base_distance_points": 500.0, "daily_stop_loss": 0, "daily_target": 0, "order_type": "MARKET",
    }

    result = run_backtest(
        DynamicStrangleStrategy, params, underlying="NIFTY", lots=1,
        start=_DAY, end=_DAY, data_root=data_root, cost_model=cost_model,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.opened_at.date() == _DAY
    assert trade.closed_at.date() == _DAY
    assert trade.opened_at.time() >= datetime.strptime("09:20", "%H:%M").time()
    assert trade.closed_at.time() >= datetime.strptime("14:45", "%H:%M").time()
    assert trade.reason == "evaluate_exit"
    assert trade.legs_opened == 2  # CE + PE, no adjustments/resets on flat spot
    assert trade.costs > 0

    # Flat premium the whole day -> the only P&L driver is slippage/costs,
    # both of which always hurt a round-trip -- so realized P&L must be
    # negative (SELL entry fills below quote, BUY-to-close fills above it).
    assert trade.realized_pnl < 0

    assert result.total_pnl == trade.realized_pnl
    assert result.win_rate == 0.0  # the one trade lost
    assert len(result.equity_curve) == len(_day_timestamps())
    # Once the trade is closed and nothing is open, equity is flat at the final realized P&L.
    assert result.equity_curve[-1][1] == pytest.approx(trade.realized_pnl)


def test_no_data_raises_a_clear_error(tmp_path: Path):
    with pytest.raises(ValueError, match="No NIFTY data"):
        run_backtest(
            DynamicStrangleStrategy, {"underlying": "NIFTY"}, underlying="NIFTY",
            start=date(2020, 1, 1), end=date(2020, 1, 2), data_root=tmp_path,
        )


def test_close_all_stays_blocked_before_stale_close_days_elapse(data_root: Path):
    """A leg with no fresh price available shouldn't be force-closed on
    the very first failed attempt -- only once it's been unpriceable for
    a real stretch (stale_close_days). Strike 30000 has no downloaded
    series at all in this fixture (unlike 25250/24750, which do) -- same
    "drifted onto an offset nothing was ever downloaded for" scenario
    found in the real Iron Fly Adjustments stuck-trade case (_asof
    returns a series' last known value indefinitely once it exists, so
    it's the *absence* of any series at all that makes a leg unpriceable,
    not merely running past the end of one that does exist)."""
    ds = HistoricalDataSource("NIFTY", data_root)
    runner = _Runner(ds, CostModel(), stale_close_days=5)
    entry_leg = OrderLeg(
        label="SELL 30000 CE", security_id=ds.security_id_for(30000, "CE"), trading_symbol="NIFTY 30000 CE x",
        exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75, order_type="MARKET",
        product_type="INTRADAY", price=60.0, role="primary",
    )
    runner.start_run(datetime(2024, 9, 4, 9, 20, tzinfo=IST), [entry_leg])

    later_ts = int(datetime(2024, 9, 6, 9, 20, tzinfo=IST).timestamp())  # 2 days later, < stale_close_days
    assert runner.close_all(later_ts, date(2024, 9, 6)) is False
    assert runner.is_open is True


def test_close_all_force_closes_at_last_known_price_once_stale(data_root: Path):
    ds = HistoricalDataSource("NIFTY", data_root)
    runner = _Runner(ds, CostModel(), stale_close_days=5)
    entry_leg = OrderLeg(
        label="SELL 30000 CE", security_id=ds.security_id_for(30000, "CE"), trading_symbol="NIFTY 30000 CE x",
        exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75, order_type="MARKET",
        product_type="INTRADAY", price=60.0, role="primary",
    )
    runner.start_run(datetime(2024, 9, 4, 9, 20, tzinfo=IST), [entry_leg])

    # 6 days after entry -- past stale_close_days=5, and this strike never
    # had a fresh quote even once (no series at all for its offset).
    stale_ts = int(datetime(2024, 9, 10, 9, 20, tzinfo=IST).timestamp())
    closed_at = datetime(2024, 9, 10, 9, 20, tzinfo=IST)
    result = runner.close_all(stale_ts, closed_at.date())

    assert result is True
    assert runner.used_stale_price is True
    trade = runner.finish(closed_at, "evaluate_exit")
    assert trade.used_stale_price is True
    # Closed at the SAME last-known (entry fill) price, both sides only
    # slippage-adjusted -- near zero before costs, so realized_pnl (net
    # of costs) should land close to -costs, not some fabricated number
    # far away from that.
    assert trade.realized_pnl == pytest.approx(-trade.costs, abs=5)


def test_apply_rolls_reads_the_nested_rolls_list(data_root: Path):
    """Regression: evaluate_rolls returns {"rolls": [roll, ...]} -- a list
    of independent roll operations, not a flat {"close_security_ids": ...}
    dict at the top level (confirmed against app.engine.runner._apply_rolls's
    own real contract). _Runner.apply_rolls once read close_security_ids
    off the wrong level and silently did nothing on every real roll --
    only surfaced as multi-month-long "trades" when run against the real
    2-year dataset, since a position that can never adjust/reset just sits
    open until its strikes eventually drift out of the downloaded band
    and can't even be priced to force-close anymore."""
    ds = HistoricalDataSource("NIFTY", data_root)
    runner = _Runner(ds, CostModel())
    runner.opened_at = datetime(2024, 9, 4, 9, 20, tzinfo=IST)

    old_leg = {
        "label": "SELL 25500 CE", "security_id": ds.security_id_for(25500, "CE"), "trading_symbol": "NIFTY 25500 CE x",
        "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 75, "order_type": "MARKET",
        "product_type": "INTRADAY", "price": 60.0, "role": "primary",
    }
    runner.legs = [old_leg]
    runner.leg_state = {}

    new_leg = OrderLeg(
        label="RESET SELL 24500 CE", security_id=ds.security_id_for(24500, "CE"), trading_symbol="NIFTY 24500 CE x",
        exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75, order_type="MARKET",
        product_type="INTRADAY", price=45.0, role="primary",
    )
    decision = {"rolls": [{"close_security_ids": [old_leg["security_id"]], "new_legs": [new_leg]}]}

    ts = int(datetime(2024, 9, 4, 9, 20, tzinfo=IST).timestamp())
    runner.apply_rolls(ts, date(2024, 9, 4), decision)

    assert runner.leg_state[old_leg["security_id"]]["status"] == "closed"
    assert len(runner.legs) == 2  # old (now closed) + the new one appended
    assert runner.legs[-1]["security_id"] == new_leg.security_id
    assert runner.legs_opened_count == 1  # only the new leg counts as a fresh open
