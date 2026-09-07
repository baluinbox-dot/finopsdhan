"""CLI: run a Dynamic Strangle backtest sweep across base_distance values
and build a self-contained HTML report (equity curve, parameter
comparison, trade log) -- Phase 3 of the backtest effort. See
scripts/backtest/run_backtest.py for a single plain-text run, and
app/backtest/engine.py for the engine itself (Phase 2).

Usage:
    python scripts/backtest/generate_report.py --underlying NIFTY \
        --base-distances 500,1000,1500,2000 --start 2024-09-04 --end 2026-09-07 \
        --out scripts/backtest/report_output.html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backtest.engine import run_backtest  # noqa: E402
from app.strategies.dynamic_strangle import DynamicStrangleStrategy  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "backtest"
TEMPLATE_PATH = Path(__file__).resolve().parent / "report_template.html"

# The trade log only ever shows the most recent N trades for whichever
# base_distance is selected in the report's own UI -- keeps the embedded
# JSON a sane size on a 2-year sweep (full history is still in the raw
# --json-out file, if kept, for anything needing more than a spot-check).
_TRADES_SHOWN_PER_RUN = 40


def _run_one(underlying: str, base_distance: float, lots: int, start: date, end: date) -> dict:
    params = {
        "underlying": underlying,
        # Must be non-empty -- dynamic_strangle.py guards entry on this
        # being truthy, but the backtest data source itself never reads
        # the value (a synthetic chain snapshot is purely a function of
        # the simulated timestamp, not of which expiry string was asked for).
        "expiry": "backtest",
        "lots": lots,
        "start_time": "09:20",
        "end_time": "14:45",
        "base_distance_points": base_distance,
        "daily_stop_loss": 0,
        "daily_target": 0,
        "order_type": "MARKET",
    }
    result = run_backtest(
        DynamicStrangleStrategy, params, underlying=underlying, lots=lots, start=start, end=end, data_root=DATA_ROOT,
    )
    return {
        "base_distance": base_distance,
        "trade_count": len(result.trades),
        "win_rate": round(result.win_rate, 1) if result.win_rate is not None else None,
        "total_pnl": round(result.total_pnl),
        "total_costs": round(sum(t.costs for t in result.trades)),
        "max_drawdown": round(result.max_drawdown),
        "avg_pnl_per_trade": round(result.total_pnl / len(result.trades)) if result.trades else None,
        "recent_trades": [
            {
                "opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(), "reason": t.reason,
                "realized_pnl": round(t.realized_pnl), "costs": round(t.costs), "legs_opened": t.legs_opened,
            }
            for t in result.trades[-_TRADES_SHOWN_PER_RUN:]
        ],
        "equity_curve_daily": _downsample_daily(result.equity_curve),
    }


def _downsample_daily(equity_curve: list[tuple]) -> list[dict]:
    """One point per trading day (the day's last tick) -- a 2-year run at
    5-min granularity is thousands of points, far more than a report
    chart needs; this keeps both the chart and the embedded JSON a sane
    size."""
    by_day: dict[str, float] = {}
    for ts, pnl in equity_curve:
        by_day[ts.date().isoformat()] = round(pnl)  # later ticks overwrite earlier ones -> last tick of the day wins
    return [{"date": d, "pnl": pnl} for d, pnl in sorted(by_day.items())]


def build_report(underlying: str, base_distances: list[float], lots: int, start: date, end: date, out: Path, json_out: Path | None) -> None:
    runs = []
    for bd in base_distances:
        print(f"Running base_distance={bd}...", file=sys.stderr)
        runs.append(_run_one(underlying, bd, lots, start, end))
        print(f"  -> {runs[-1]['trade_count']} trades, Rs {runs[-1]['total_pnl']:,.0f} net", file=sys.stderr)

    headline = max(runs, key=lambda r: r["total_pnl"])
    report_data = {
        "underlying": underlying, "start": start.isoformat(), "end": end.isoformat(),
        "headline_base_distance": headline["base_distance"], "runs": runs,
    }

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report_data, indent=2))
        print(f"Wrote {json_out}", file=sys.stderr)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__REPORT_DATA__", json.dumps(report_data, separators=(",", ":")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Dynamic Strangle backtest sweep and build an HTML report.")
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "SENSEX"], required=True)
    parser.add_argument("--base-distances", default="500", help="comma-separated list, e.g. 500,1000,1500,2000")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 9, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out", type=Path, required=True, help="path to write the final HTML report")
    parser.add_argument("--json-out", type=Path, default=None, help="optional: also write the raw report data as JSON")
    args = parser.parse_args()

    base_distances = [float(x) for x in args.base_distances.split(",")]
    build_report(args.underlying, base_distances, args.lots, args.start, args.end, args.out, args.json_out)


if __name__ == "__main__":
    main()
