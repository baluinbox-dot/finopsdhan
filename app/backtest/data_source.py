"""Reconstructs live-shaped Dhan data (a chain DataFrame, a spot price, a
leg's current premium) from the local historical CSV cache, at any
simulated timestamp within the downloaded range.

The core wrinkle this works around: Dhan's `expired_options_data` with
`strike="ATM+5"` is NOT one fixed contract's price history -- it's
"whichever strike sits 5 chain-positions above spot at this exact
candle," recomputed every candle (confirmed empirically during Phase 0/1
of this backtest effort). So a strategy's actual entered strike (say
25,250) starts as "ATM+5" and, as spot drifts over a multi-day hold,
could become "ATM-3" a week later relative to a new ATM. To track one
specific strike's premium across time, every downloaded offset series is
combined at each queried timestamp: each series' own `spot` column says
what ATM was at that candle, which converts that series' offset into a
real absolute strike for that moment -- giving a synthetic chain
snapshot (`{strike: {ce, pe}}`) exactly like a live `fetch_chain_df` call
would return, but assembled from up to ~20 separate CSV series instead
of one live API response.

Real, bounded limitation this carries forward: only strikes within the
downloaded offset band (see scripts/backtest/download_historical_data.py
-- roughly ATM+-10 reliably, wider offsets are mostly empty/illiquid and
were never written to disk) are reconstructable. A position that drifts
outside that band loses price data mid-backtest -- `quote_for` and
`chain_at` both return None for anything outside it, and every strategy
here already treats "no fresh quote" as "skip this poll, don't guess,"
so this degrades safely rather than fabricating a price, but it does
mean a strategy that trends hard and far in one direction for weeks
can't be faithfully backtested with this offset band as downloaded.

Synthetic security_ids (`f"{underlying}|{int(strike)}|{side}"`) stand in
for real Dhan contract ids -- fine since backtesting never places a real
order, only needs a stable, unique key for currently_open_legs/leg_state
to key off, exactly the role a real security_id already plays.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

# Confirmed from this app's own live strike selection across every
# strategy (see app.dhan.helpers.UNDERLYINGS and the strikes actually
# returned by fetch_chain_df in production) -- not re-derived from the
# downloaded data itself, which doesn't carry the interval explicitly.
STRIKE_INTERVAL: dict[str, int] = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}

# Real lot sizes drift over time (SEBI-mandated changes have moved
# NIFTY's more than once across the 2-year backtest window) -- this app
# has no historical lot-size record, only whatever get_lot_size resolves
# live *today*. Backtesting a 2-year window with today's lot size is a
# known, deliberate simplification: position *sizing* in rupee P&L terms
# will drift from what a real historical fill would have sized to on an
# old lot size, but every leg within one backtest is still internally
# consistent (same size in, same size out). Revisit if/when a historical
# lot-size table is ever sourced.
DEFAULT_LOT_SIZE: dict[str, int] = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20}

_OFFSET_DIR_RE = re.compile(r"^ATM([+-]\d+)?$")

# Synthetic security_id encoding: several strategies call int(leg["security_id"])
# when building a fetch_quotes request (e.g. "securities_by_segment...append
# (int(leg["security_id"]))"), so a synthetic id must itself be a plain
# integer string, not something human-readable like "NIFTY|25250|CE" (which
# would raise ValueError the first time any strategy tried that cast).
# Packed as: underlying_code * 10_000_000 + int(strike) * 10 + side_code --
# deterministic and exactly reversible, comfortably fits strikes up to
# 999,999 (SENSEX today is ~90,000) in the 6 digits above the side digit.
_UNDERLYING_CODE = {"NIFTY": 1, "BANKNIFTY": 2, "SENSEX": 3}
_CODE_UNDERLYING = {v: k for k, v in _UNDERLYING_CODE.items()}
_SIDE_CODE = {"CE": 0, "PE": 1}
_CODE_SIDE = {v: k for k, v in _SIDE_CODE.items()}


def _offset_from_dirname(name: str) -> int | None:
    match = _OFFSET_DIR_RE.match(name)
    if not match:
        return None
    return int(match.group(1)) if match.group(1) else 0


def _nearest_strike(spot: float, interval: int) -> float:
    return round(spot / interval) * interval


class HistoricalDataSource:
    """One instance per underlying. Loads every downloaded CSV for it
    once (spot + every available offset/side series), then answers
    point-in-time queries against that in-memory data -- no disk I/O per
    query, so a backtest's tick loop (thousands of queries) stays fast."""

    def __init__(self, underlying: str, data_root: Path | str):
        self.underlying = underlying.upper()
        if self.underlying not in STRIKE_INTERVAL:
            raise ValueError(f"No strike interval known for {underlying!r}")
        self.interval = STRIKE_INTERVAL[self.underlying]
        self._root = Path(data_root) / self.underlying

        self._spot_ts, self._spot_close = self._load_spot()
        # {(side, offset): (sorted_ts_array, close_array, spot_array)}
        self._options: dict[tuple[str, int], tuple[np.ndarray, np.ndarray, np.ndarray]] = self._load_options()

    # --- loading ---

    def _load_spot(self) -> tuple[np.ndarray, np.ndarray]:
        spot_dir = self._root / "spot_5min"
        if not spot_dir.is_dir():
            return np.array([]), np.array([])
        frames = [pd.read_csv(f) for f in sorted(spot_dir.glob("*.csv"))]
        if not frames:
            return np.array([]), np.array([])
        df = pd.concat(frames, ignore_index=True).dropna(subset=["timestamp", "close"])
        df = df.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last")
        return df["timestamp"].to_numpy(dtype="int64"), df["close"].to_numpy(dtype="float64")

    def _load_options(self) -> dict[tuple[str, int], tuple[np.ndarray, np.ndarray, np.ndarray]]:
        result: dict[tuple[str, int], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for side, side_dir_name in (("CE", "CE"), ("PE", "PE")):
            side_dir = self._root / "options" / side_dir_name
            if not side_dir.is_dir():
                continue
            for offset_dir in side_dir.iterdir():
                offset = _offset_from_dirname(offset_dir.name)
                if offset is None or not offset_dir.is_dir():
                    continue
                frames = [pd.read_csv(f) for f in sorted(offset_dir.glob("*.csv"))]
                if not frames:
                    continue
                df = pd.concat(frames, ignore_index=True).dropna(subset=["timestamp", "close", "spot"])
                if df.empty:
                    continue
                df = df.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last")
                result[(side, offset)] = (
                    df["timestamp"].to_numpy(dtype="int64"),
                    df["close"].to_numpy(dtype="float64"),
                    df["spot"].to_numpy(dtype="float64"),
                )
        return result

    # --- point-in-time lookups ---

    @staticmethod
    def _asof(ts_array: np.ndarray, value_array: np.ndarray, ts: int) -> float | None:
        """Value at the candle at-or-before `ts` (last-known-value, same
        semantics as a live quote that's a few seconds stale) -- None if
        `ts` is before the series' first candle."""
        if ts_array.size == 0:
            return None
        idx = np.searchsorted(ts_array, ts, side="right") - 1
        if idx < 0:
            return None
        return float(value_array[idx])

    def spot_at(self, ts: int) -> float | None:
        return self._asof(self._spot_ts, self._spot_close, ts)

    def timestamps(self) -> np.ndarray:
        """Every spot candle timestamp in the loaded range, ascending --
        the tick grid a backtest loop steps through."""
        return self._spot_ts

    def daily_closes(self, as_of: date, lookback_days: int = 90) -> list[float]:
        """Daily closing prices up to and including `as_of` (oldest
        first), over the last `lookback_days` calendar days -- the last
        5-min candle of each IST trading day counts as that day's close.
        Mirrors app.dhan.helpers.fetch_daily_closes's own contract
        (oldest-first list of floats) exactly, so a strategy computing an
        indicator off it (e.g. RSI, see app.strategies.rsi_call_writing)
        needs no changes to work against this instead of live Dhan."""
        if self._spot_ts.size == 0:
            return []
        start_ts = int(datetime(as_of.year, as_of.month, as_of.day, tzinfo=IST).timestamp()) - lookback_days * 86400
        end_ts = int(datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=IST).timestamp())
        mask = (self._spot_ts >= start_ts) & (self._spot_ts <= end_ts)
        ts_in_range = self._spot_ts[mask]
        close_in_range = self._spot_close[mask]
        if ts_in_range.size == 0:
            return []
        # _spot_ts is sorted ascending, so iterating in order and
        # overwriting by_day[d] naturally keeps each day's LAST candle.
        by_day: dict[date, float] = {}
        for t, c in zip(ts_in_range, close_in_range):
            d = datetime.fromtimestamp(int(t), tz=IST).date()
            by_day[d] = float(c)
        return [by_day[d] for d in sorted(by_day)]

    def security_id_for(self, strike: float, side: str) -> str:
        code = _UNDERLYING_CODE[self.underlying]
        return str(code * 10_000_000 + int(strike) * 10 + _SIDE_CODE[side])

    @staticmethod
    def parse_security_id(security_id: str) -> tuple[str, float, str] | None:
        try:
            n = int(security_id)
        except (TypeError, ValueError):
            return None
        code, remainder = divmod(n, 10_000_000)
        strike, side_code = divmod(remainder, 10)
        if code not in _CODE_UNDERLYING or side_code not in _CODE_SIDE:
            return None
        return _CODE_UNDERLYING[code], float(strike), _CODE_SIDE[side_code]

    def _offset_for_strike(self, strike: float, reference_spot: float) -> int:
        return round((strike - _nearest_strike(reference_spot, self.interval)) / self.interval)

    # How many strikes off the exact computed offset quote_for will search
    # before giving up. Real, expected gaps exist *inside* the downloaded
    # band, not just at its edges -- confirmed on NIFTY: CE offsets +11,
    # +12, +15 came back with zero rows across the entire 2-year pull
    # (Phase 1 download), i.e. genuinely no liquidity/data at Dhan's own
    # source, not a download failure. A leg entered well within the band
    # routinely drifts onto one of these as spot moves even a few hundred
    # points -- without this fallback, that leg becomes unpriceable (can't
    # be marked-to-market, can't be closed, can't be rolled away from)
    # for the rest of the backtest the moment it lands on a gap, which is
    # far too fragile to be usable. 3 strikes is a deliberately small
    # search radius -- close enough that using an adjacent strike's
    # premium as a stand-in is a reasonable approximation, not a guess
    # from an unrelated part of the chain.
    _MAX_OFFSET_FALLBACK_SEARCH = 3

    def quote_for(self, security_id: str, ts: int) -> float | None:
        """Current premium for a leg previously opened via `chain_at`'s
        synthetic security_id, at time `ts`. Falls back to the nearest
        available offset (see _MAX_OFFSET_FALLBACK_SEARCH) if the exact
        computed one has no data -- an approximation, not the true price
        of that exact strike, but real gaps inside the downloaded band
        make that necessary (see the constant's own docstring). Returns
        None (never fabricated beyond that search radius) if `ts`
        predates the data, or the strike has drifted too far outside the
        downloaded offset band entirely -- callers must treat that
        exactly like a live failed quote fetch (skip this poll, don't
        guess)."""
        parsed = self.parse_security_id(security_id)
        if parsed is None or parsed[0] != self.underlying:
            return None
        _, strike, side = parsed
        spot = self.spot_at(ts)
        if spot is None:
            return None
        offset = self._offset_for_strike(strike, spot)
        for candidate in self._offsets_by_distance(offset):
            series = self._options.get((side, candidate))
            if series is None:
                continue
            ts_array, close_array, _ = series
            price = self._asof(ts_array, close_array, ts)
            if price is not None:
                return price
        return None

    def _offsets_by_distance(self, offset: int):
        """`offset`, then the nearest ones outward on alternating sides,
        up to _MAX_OFFSET_FALLBACK_SEARCH away -- the search order for
        quote_for's fallback."""
        yield offset
        for delta in range(1, self._MAX_OFFSET_FALLBACK_SEARCH + 1):
            yield offset + delta
            yield offset - delta

    def chain_at(self, ts: int) -> tuple[pd.DataFrame, float] | None:
        """(chain_df, spot) shaped exactly like a live
        app.dhan.helpers.fetch_chain_df call -- strike, ce_security_id,
        ce_ltp, pe_security_id, pe_ltp columns (the only ones any
        strategy in this app actually reads off chain_df). None if there
        is no spot candle at or before `ts`."""
        spot = self.spot_at(ts)
        if spot is None:
            return None

        rows: dict[float, dict] = {}
        for (side, offset), (ts_array, close_array, spot_array) in self._options.items():
            idx = np.searchsorted(ts_array, ts, side="right") - 1
            if idx < 0:
                continue
            candle_spot = float(spot_array[idx])
            strike = _nearest_strike(candle_spot, self.interval) + offset * self.interval
            row = rows.setdefault(strike, {"strike": strike, "ce_security_id": None, "ce_ltp": None, "pe_security_id": None, "pe_ltp": None})
            price = float(close_array[idx])
            row[f"{side.lower()}_security_id"] = self.security_id_for(strike, side)
            row[f"{side.lower()}_ltp"] = price

        if not rows:
            return None
        df = pd.DataFrame(sorted(rows.values(), key=lambda r: r["strike"]))
        return df, spot
