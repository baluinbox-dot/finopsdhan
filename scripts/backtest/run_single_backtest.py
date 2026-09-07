"""Subprocess entrypoint for one in-app backtest request (Phase C -- see
app.routers.backtest). Invoked as:

    python scripts/backtest/run_single_backtest.py <backtest_run_id>

spawned via subprocess.Popen (detached, `start_new_session=True`) so a
potentially few-hundred-MB pandas load (see app.backtest.data_source's own
"loads every downloaded CSV once" docstring) never happens inside the
live-trading uvicorn process -- this app's deploy VM has a documented
history of near-OOM incidents. There is no request to respond to once this
is running in the background, so the only channel back to the app is the
`backtest_runs` row itself: this script owns writing its status/result/
error_message as it goes.

A hard RLIMIT_AS memory cap is set before doing any real work, so a
runaway load fails fast with a catchable MemoryError (stored as a normal
"failed" run with a clear message) instead of risking the OS OOM-killer
taking down other processes on the box. POSIX-only -- a no-op on Windows
(local dev), which is fine since only the Linux VM needs this cap for real.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `import app...` resolves

from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.engine import run_backtest  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import BacktestRun  # noqa: E402
from app.strategies.registry import AUTO_ADVANCE_EXPIRY_STRATEGIES, get_strategy_class  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_single_backtest")

DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "backtest"

# 768MB -- generous over the ~250-300MB one HistoricalDataSource instance
# needs for a single underlying's full downloaded range (~85MB of CSV on
# disk typically expands a few times over once loaded into pandas), but
# comfortably under this VM's total RAM alongside the live app and
# everything else already running on it.
_MEMORY_LIMIT_BYTES = 768 * 1024 * 1024


def _apply_memory_limit() -> None:
    try:
        import resource
    except ImportError:
        return  # Windows -- no hard cap locally, only the Linux VM needs this for real
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    except (ValueError, OSError):
        logger.warning("Could not set RLIMIT_AS -- continuing without a hard memory cap")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _downsample_equity_curve(equity_curve: list[tuple[datetime, float]]) -> list[list]:
    """One point per IST calendar day (the day's last tick) -- a multi-year
    backtest's real equity curve has thousands of 5-min-tick points, far
    more resolution than a summary results page needs, and storing all of
    it in this row's JSON column would bloat it for no real benefit."""
    by_day: dict[date, tuple[datetime, float]] = {}
    for ts, pnl in equity_curve:
        by_day[ts.date()] = (ts, pnl)  # later ticks overwrite earlier ones for the same day -> last wins
    return [[ts.isoformat(), pnl] for ts, pnl in sorted(by_day.values(), key=lambda pair: pair[0])]


def main(run_id: str) -> None:
    _apply_memory_limit()
    db = SessionLocal()
    try:
        run = db.get(BacktestRun, uuid.UUID(run_id))
        if run is None:
            logger.error("BacktestRun %s not found -- nothing to do", run_id)
            return

        run.status = "running"
        run.started_at = _now()
        db.commit()

        try:
            strategy_cls = get_strategy_class(run.strategy.code_ref)
            result = run_backtest(
                strategy_cls, dict(run.params), underlying=run.underlying,
                lots=int(run.params.get("lots") or 1), start=run.start_date, end=run.end_date,
                data_root=DATA_ROOT, cost_model=CostModel(),
                auto_advance_expiry=run.strategy.code_ref in AUTO_ADVANCE_EXPIRY_STRATEGIES,
            )
        except Exception as exc:  # noqa: BLE001 -- any failure here must land as a "failed" row, not crash silently
            logger.exception("Backtest run %s failed", run_id)
            run.status = "failed"
            run.error_message = f"{type(exc).__name__}: {exc}"
            run.finished_at = _now()
            db.commit()
            return

        run.status = "completed"
        run.finished_at = _now()
        run.result = {
            "trade_count": len(result.trades),
            "total_pnl": result.total_pnl,
            "win_rate": result.win_rate,
            "max_drawdown": result.max_drawdown,
            "stale_trade_count": sum(1 for t in result.trades if t.used_stale_price),
            "equity_curve": _downsample_equity_curve(result.equity_curve),
            "trades": [
                {
                    "opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(),
                    "reason": t.reason, "realized_pnl": t.realized_pnl, "costs": t.costs,
                    "legs_opened": t.legs_opened, "used_stale_price": t.used_stale_price,
                }
                for t in result.trades
            ],
        }
        db.commit()
        logger.info(
            "Backtest run %s completed: %d trades, total_pnl=%.2f",
            run_id, len(result.trades), result.total_pnl,
        )
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/backtest/run_single_backtest.py <backtest_run_id>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
