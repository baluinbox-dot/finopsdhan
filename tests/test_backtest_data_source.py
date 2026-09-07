from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.backtest.data_source import HistoricalDataSource


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "backtest"
    # Spot: three 5-min candles, NIFTY, spot 25000 flat.
    _write_csv(root / "NIFTY" / "spot_5min" / "chunk.csv", [
        {"timestamp": 1000, "open": 25000, "high": 25000, "low": 25000, "close": 25000.0, "volume": 0},
        {"timestamp": 1300, "open": 25000, "high": 25000, "low": 25000, "close": 25010.0, "volume": 0},
        {"timestamp": 1600, "open": 25000, "high": 25000, "low": 25000, "close": 25020.0, "volume": 0},
    ])
    # ATM+5 CE: strike = nearest_strike(spot,50)+250 = 25000+250=25250 at spot 25000/25010; 25260->25250 still (round(25010/50)*50=25000).
    _write_csv(root / "NIFTY" / "options" / "CE" / "ATM+5" / "chunk.csv", [
        {"timestamp": 1000, "open": 60, "high": 60, "low": 60, "close": 60.0, "oi": 100, "spot": 25000.0},
        {"timestamp": 1300, "open": 55, "high": 55, "low": 55, "close": 55.0, "oi": 100, "spot": 25010.0},
        {"timestamp": 1600, "open": 50, "high": 50, "low": 50, "close": 50.0, "oi": 100, "spot": 25020.0},
    ])
    _write_csv(root / "NIFTY" / "options" / "PE" / "ATM-5" / "chunk.csv", [
        {"timestamp": 1000, "open": 40, "high": 40, "low": 40, "close": 40.0, "oi": 100, "spot": 25000.0},
        {"timestamp": 1300, "open": 42, "high": 42, "low": 42, "close": 42.0, "oi": 100, "spot": 25010.0},
        {"timestamp": 1600, "open": 45, "high": 45, "low": 45, "close": 45.0, "oi": 100, "spot": 25020.0},
    ])
    return root


def test_spot_at_returns_nearest_at_or_before_candle(data_root: Path):
    ds = HistoricalDataSource("NIFTY", data_root)
    assert ds.spot_at(1000) == 25000.0
    assert ds.spot_at(1250) == 25000.0  # between candles -> last known (1000's)
    assert ds.spot_at(1300) == 25010.0
    assert ds.spot_at(999) is None  # before any data


def test_security_id_round_trips():
    ds = HistoricalDataSource("NIFTY", Path("/nonexistent"))  # no data needed for this
    sid = ds.security_id_for(25250, "CE")
    assert int(sid) > 0  # must be a plain integer string -- several strategies call int(security_id)
    assert ds.parse_security_id(sid) == ("NIFTY", 25250.0, "CE")


def test_chain_at_reconstructs_strikes_from_spot_and_offset(data_root: Path):
    ds = HistoricalDataSource("NIFTY", data_root)
    result = ds.chain_at(1000)
    assert result is not None
    chain_df, spot = result
    assert spot == 25000.0
    # ATM+5 at spot 25000 -> nearest_strike(25000,50)=25000, +5*50=25250
    ce_row = chain_df[chain_df["strike"] == 25250]
    assert not ce_row.empty
    assert ce_row.iloc[0]["ce_ltp"] == 60.0
    # ATM-5 at spot 25000 -> 25000-250=24750
    pe_row = chain_df[chain_df["strike"] == 24750]
    assert not pe_row.empty
    assert pe_row.iloc[0]["pe_ltp"] == 40.0


def test_quote_for_tracks_a_strike_as_spot_moves(data_root: Path):
    """The strike 25250 stays put; spot moving from 25000 to 25010 doesn't
    change which absolute strike ATM+5 means at that later candle (still
    rounds to 25000 base -> 25250), so the same strike's price should be
    trackable across both ticks via the SAME series."""
    ds = HistoricalDataSource("NIFTY", data_root)
    sid = ds.security_id_for(25250, "CE")
    assert ds.quote_for(sid, 1000) == 60.0
    assert ds.quote_for(sid, 1300) == 55.0


def test_quote_for_none_when_strike_outside_downloaded_band(data_root: Path):
    ds = HistoricalDataSource("NIFTY", data_root)
    sid = ds.security_id_for(30000, "CE")  # way outside ATM+5's reach, and outside the fallback search radius too
    assert ds.quote_for(sid, 1000) is None


def test_quote_for_falls_back_to_nearest_available_offset(data_root: Path):
    """Regression for a real gap found running a live backtest: a leg
    drifts (as spot moves) onto an offset that was genuinely never
    downloaded (some strikes have zero data at the source, confirmed on
    real NIFTY CE +11/+12/+15) -- without a fallback, that leg becomes
    permanently unpriceable (can't mark-to-market, can't close, can't
    roll away from it) the instant it lands there. ATM+6 has no series in
    this fixture at all (only ATM+5 does); a strike that computes to
    offset+6 should still price off ATM+5 instead, since it's within the
    fallback's small search radius."""
    ds = HistoricalDataSource("NIFTY", data_root)
    # nearest_strike(25000,50)=25000; offset+6 -> 25300. No ATM+6 series
    # exists in this fixture -- only ATM+5 (25250) does.
    sid = ds.security_id_for(25300, "CE")
    assert ds.quote_for(sid, 1000) == 60.0  # ATM+5's price, used as the nearest stand-in


def test_quote_for_none_for_a_different_underlying():
    ds = HistoricalDataSource("BANKNIFTY", Path("/nonexistent"))
    nifty_sid = HistoricalDataSource("NIFTY", Path("/nonexistent")).security_id_for(25250, "CE")
    assert ds.quote_for(nifty_sid, 1000) is None


@pytest.fixture()
def multi_day_data_root(tmp_path: Path) -> Path:
    """Three trading days, one candle per day (10:00 IST), each a
    distinct close -- realistic epoch timestamps this time, since
    daily_closes needs real calendar dates to group by."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    root = tmp_path / "backtest"
    days = [
        (datetime(2024, 9, 4, 10, 0, tzinfo=ist), 25000.0),
        (datetime(2024, 9, 4, 14, 0, tzinfo=ist), 25050.0),  # same day, later -> day's close should be this one
        (datetime(2024, 9, 5, 10, 0, tzinfo=ist), 25100.0),
        (datetime(2024, 9, 6, 10, 0, tzinfo=ist), 25200.0),
    ]
    _write_csv(root / "NIFTY" / "spot_5min" / "chunk.csv", [
        {"timestamp": int(dt.timestamp()), "open": px, "high": px, "low": px, "close": px, "volume": 0}
        for dt, px in days
    ])
    return root


def test_daily_closes_takes_the_last_candle_of_each_day(multi_day_data_root: Path):
    from datetime import date

    ds = HistoricalDataSource("NIFTY", multi_day_data_root)
    closes = ds.daily_closes(date(2024, 9, 6), lookback_days=90)
    assert closes == [25050.0, 25100.0, 25200.0]  # oldest first; 09-04's 14:00 candle wins over 10:00


def test_daily_closes_respects_lookback_window(multi_day_data_root: Path):
    from datetime import date

    ds = HistoricalDataSource("NIFTY", multi_day_data_root)
    # A 0-day lookback still starts at as_of's own midnight -> only 09-06 itself.
    closes = ds.daily_closes(date(2024, 9, 6), lookback_days=0)
    assert closes == [25200.0]


def test_daily_closes_empty_when_no_data():
    from datetime import date

    ds = HistoricalDataSource("NIFTY", Path("/nonexistent"))
    assert ds.daily_closes(date(2024, 9, 6)) == []
