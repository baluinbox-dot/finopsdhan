"""Synthetic weekly expiry calendar for backtesting strategies that hold
across days against a *fixed, configured* expiry (Iron Condor Rolling,
Iron Fly Adjustments) -- unlike Dynamic Strangle/3-Pair Rolling/etc.,
which never look at the expiry value itself, these check real calendar
dates (e.g. "has today passed the configured expiry?").

Dhan's own `list_expiries` is a *live* endpoint -- it returns the current
and upcoming real expiries, never a historical list, so there is no way
to recover the real historical expiry-day-of-week (which NSE/BSE have
changed more than once) from any Dhan API call, live or historical. This
generates a fixed-weekday calendar instead and holds that weekday
constant across the whole backtest window -- a documented approximation,
not a claim of historical accuracy. Pass the weekday that actually
matches the period being tested if it's known to differ from the default.
"""

from __future__ import annotations

from datetime import date, timedelta

# Monday=0 .. Sunday=6 (date.weekday() convention). Thursday was NIFTY/
# BANKNIFTY/SENSEX's long-standing weekly expiry day for most of these
# indices' history -- the sane default, not a claim it held throughout
# any specific backtest window.
DEFAULT_EXPIRY_WEEKDAY = 3  # Thursday


def weekly_expiries(start: date, end: date, weekday: int = DEFAULT_EXPIRY_WEEKDAY) -> list[str]:
    """Every occurrence of `weekday` from `start` to `end` (inclusive),
    plus a small buffer past `end` so callers advancing "to the next
    expiry after the last one in range" near the end of a backtest still
    have somewhere to advance to. ISO date strings, ascending."""
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    out = []
    d = first
    buffered_end = end + timedelta(days=14)
    while d <= buffered_end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def next_expiry_on_or_after(expiries: list[str], today: date) -> str | None:
    """The earliest expiry in `expiries` that is >= `today` -- None if
    every expiry in the (pre-generated) list has already passed."""
    for e in expiries:
        if date.fromisoformat(e) >= today:
            return e
    return None
