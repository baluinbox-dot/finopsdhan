"""Historical data downloader for backtesting (Phase 1 of the backtest plan).

Pulls, for each configured underlying (NIFTY / BANKNIFTY / SENSEX):
  - Spot 5-min candles (`intraday_minute_data`) over the full date range.
  - ATM-30..ATM+30 option premium candles (`expired_options_data`), CE and
    PE separately, weekly expiry only (`expiry_flag="WEEK"`, `expiry_code=1`
    -- confirmed empirically to mean "the nearest weekly contract as of
    each moment in the range," not a single contract pinned to today).

Not part of the live app's request path -- a standalone script, run
manually (or via cron later). Reuses the app's own DB/Dhan-client
plumbing (the same encrypted credential every live/paper strategy uses),
but does NOT coordinate with the live app's in-process Dhan throttle
registries in app.dhan.helpers -- this is a separate process with its own
(more conservative) pacing instead. Do not run this at the same time as
anything latency-sensitive on the live account without checking
_SLEEP_SECONDS is still comfortably safe.

Resumable by design: every (underlying, series, date-chunk) is written to
its own CSV file under DATA_ROOT, named by its exact chunk boundaries. A
chunk whose file already exists is skipped without an API call. Because
_BACKFILL_START is a fixed calendar date (not "N days before today,"
which would shift on every run and break this), re-running this script
on a later day only ever adds new chunks for the time that has newly
elapsed -- it never re-fetches or re-names anything already on disk. This
is what makes "run it whenever you want to refresh" (as opposed to a
strict schedule) actually safe.

CSV, not Parquet: neither pyarrow nor fastparquet is installed anywhere
in this project (checked before writing this), and installing one on the
deploy VM risks repeating the memory-heavy pip-install incidents already
documented for this VM. CSV needs nothing beyond pandas, already a
dependency. Revisit compression/format once the pipeline itself is
proven -- converting CSV -> Parquet locally later is a cheap follow-up,
not something worth the VM risk now.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.db import SessionLocal
from app.dhan.client import get_user_dhan_client
from app.dhan.helpers import UNDERLYINGS
from app.models import User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest_download")

# This script's calls are NOT coordinated with the live app's own
# per-account Dhan throttle (separate process, separate in-memory state) --
# empirically, unthrottled back-to-back calls hit DH-904 (rate limit)
# almost immediately, while a 3s gap ran 7 calls cleanly. 3.5s is a
# deliberate margin on top of that, not the tightest safe value.
_SLEEP_SECONDS = 3.5

# Empirically confirmed against Dhan's real API (not documented anywhere):
# a 45-day span succeeds, a 60-day span fails outright ("bad values for
# parameters"). 40 is a safety margin under that boundary, not the exact
# cap -- the exact cutoff between 45 and 60 was never pinned down.
_CHUNK_DAYS = 40

# Fixed, not "date.today() - timedelta(days=730)" -- a rolling start date
# would shift by one day on every future run, changing every chunk's
# filename and defeating the whole "skip what's already on disk" resume
# logic. Set once, left alone; only `today` (the end of the range) should
# ever move forward on a later run. Moved back from 2024-09-04 to
# 2024-01-01 on Balu's request 2026-09-15 -- changing this value shifts
# every chunk boundary for the whole range (chunks are computed
# sequentially from this date, not aligned to a calendar grid), so this
# is a one-time change that requires a fresh full download, not something
# to keep nudging casually. app.routers.backtest's EARLIEST_DATA_DATE
# mirrors this value for the backtest form's own date-picker floor --
# keep both in sync if this ever changes again.
_BACKFILL_START = date(2024, 1, 1)

# ATM itself (0) plus 30 strikes either side. Widened from the original
# +-15 (2026-09-07) after real backtests of long-holding strategies
# (Iron Fly Adjustments, which resets/adjusts less often and holds for
# up to a full weekly cycle) showed an 11.5% stuck-trade rate -- a leg
# drifting far enough that its strike fell entirely outside +-15 before
# the position could next adjust/reset/close, becoming permanently
# unpriceable for the rest of that backtest. +-30 isn't a guarantee
# against every possible move either, just a documented, wider margin --
# re-running after any further widening only fetches the newly-added
# offsets, same as this widening only added -30..-16 and 16..30 (the
# original +-15 data was already on disk and got skipped).
_STRIKE_OFFSETS = list(range(-30, 31))

DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "backtest"


def _strike_label(offset: int) -> str:
    if offset == 0:
        return "ATM"
    return f"ATM{'+' if offset > 0 else ''}{offset}"


def _chunks(start: date, end: date, chunk_days: int):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        yield cur, nxt
        cur = nxt


def _get_client():
    db = SessionLocal()
    user = db.query(User).filter(User.email == "baluinbox@gmail.com").first()
    if user is None:
        raise RuntimeError("baluinbox@gmail.com not found in this DB")
    return get_user_dhan_client(db, user).client


def _write_csv(path: Path, columns: dict[str, list]) -> int:
    ts = columns.get("timestamp") or []
    if not ts:
        return 0
    n = len(ts)
    df = pd.DataFrame({k: (v if v else [None] * n) for k, v in columns.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def download_options(client, underlying: str, meta: dict, start: date, end: date) -> None:
    for side, drv_option_type in (("CE", "CALL"), ("PE", "PUT")):
        for offset in _STRIKE_OFFSETS:
            label = _strike_label(offset)
            for chunk_start, chunk_end in _chunks(start, end, _CHUNK_DAYS):
                out_path = (
                    DATA_ROOT / underlying / "options" / side / label
                    / f"{chunk_start.isoformat()}_{chunk_end.isoformat()}.csv"
                )
                if out_path.exists():
                    continue
                tag = f"{underlying} {side} {label} {chunk_start}->{chunk_end}"
                try:
                    resp = client.expired_options_data(
                        security_id=meta["security_id"], exchange_segment=meta["option_segment"],
                        instrument_type="OPTIDX", expiry_flag="WEEK", expiry_code=1,
                        strike=label, drv_option_type=drv_option_type,
                        required_data=["open", "high", "low", "close", "oi", "spot"],
                        from_date=chunk_start.isoformat(), to_date=chunk_end.isoformat(), interval=5,
                    )
                except Exception as exc:  # noqa: BLE001 -- one bad chunk must never abort the whole run
                    logger.warning("EXC %s: %s: %s", tag, type(exc).__name__, exc)
                    time.sleep(_SLEEP_SECONDS)
                    continue
                if resp.get("status") != "success":
                    logger.warning("FAIL %s: %s", tag, resp.get("remarks"))
                    time.sleep(_SLEEP_SECONDS)
                    continue
                inner = (resp.get("data") or {}).get("data") or {}
                side_data = inner.get("ce") or inner.get("pe") or {}
                n = _write_csv(out_path, {
                    "timestamp": side_data.get("timestamp"), "open": side_data.get("open"),
                    "high": side_data.get("high"), "low": side_data.get("low"),
                    "close": side_data.get("close"), "oi": side_data.get("oi"), "spot": side_data.get("spot"),
                })
                logger.info("OK %s: %d rows", tag, n)
                time.sleep(_SLEEP_SECONDS)


def download_spot(client, underlying: str, meta: dict, start: date, end: date) -> None:
    for chunk_start, chunk_end in _chunks(start, end, _CHUNK_DAYS):
        out_path = DATA_ROOT / underlying / "spot_5min" / f"{chunk_start.isoformat()}_{chunk_end.isoformat()}.csv"
        if out_path.exists():
            continue
        tag = f"{underlying} spot {chunk_start}->{chunk_end}"
        try:
            resp = client.intraday_minute_data(
                security_id=meta["security_id"], exchange_segment=meta["exchange_segment"], instrument_type="INDEX",
                from_date=chunk_start.isoformat(), to_date=chunk_end.isoformat(), interval=5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("EXC %s: %s: %s", tag, type(exc).__name__, exc)
            time.sleep(_SLEEP_SECONDS)
            continue
        if resp.get("status") != "success":
            logger.warning("FAIL %s: %s", tag, resp.get("remarks"))
            time.sleep(_SLEEP_SECONDS)
            continue
        data = resp.get("data") or {}
        n = _write_csv(out_path, {
            "timestamp": data.get("timestamp"), "open": data.get("open"), "high": data.get("high"),
            "low": data.get("low"), "close": data.get("close"), "volume": data.get("volume"),
        })
        logger.info("OK %s: %d rows", tag, n)
        time.sleep(_SLEEP_SECONDS)


def main() -> None:
    client = _get_client()
    today = date.today()

    for underlying in ("NIFTY", "BANKNIFTY", "SENSEX"):
        meta = UNDERLYINGS[underlying]
        logger.info("=== %s: spot ===", underlying)
        download_spot(client, underlying, meta, _BACKFILL_START, today)
        logger.info("=== %s: options ===", underlying)
        download_options(client, underlying, meta, _BACKFILL_START, today)

    logger.info("ALL DONE")


if __name__ == "__main__":
    main()
