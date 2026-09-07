"""CLI: run a backtest against the local historical cache and print a
report. Phase 2/3 of the backtest effort -- see
scripts/backtest/download_historical_data.py for Phase 1 (the data
itself).

Usage (from the repo root, inside the venv):
    python scripts/backtest/run_backtest.py --strategy dynamic_strangle \
        --underlying NIFTY --base-distance 500 --start 2024-09-04 --end 2026-09-07
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `import app...` resolves

from app.backtest.engine import run_backtest  # noqa: E402
from app.strategies.dynamic_strangle import DynamicStrangleStrategy  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "backtest"

_STRATEGIES = {
    "dynamic_strangle": DynamicStrangleStrategy,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a backtest against the local historical cache.")
    parser.add_argument("--strategy", choices=sorted(_STRATEGIES), required=True)
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "SENSEX"], required=True)
    parser.add_argument("--base-distance", type=float, default=500, help="Dynamic Strangle's base_distance_points")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 9, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--start-time", default="09:20")
    parser.add_argument("--end-time", default="14:45")
    args = parser.parse_args()

    params = {
        "underlying": args.underlying,
        # Must be non-empty -- dynamic_strangle.py guards entry on this
        # being truthy, but the backtest data source itself never reads
        # the value (a synthetic chain snapshot is purely a function of
        # the simulated timestamp, not of which expiry string was asked for).
        "expiry": "backtest",
        "lots": args.lots,
        "start_time": args.start_time,
        "end_time": args.end_time,
        "base_distance_points": args.base_distance,
        "daily_stop_loss": 0,  # disabled by default for a raw backtest -- override via flags later if wanted
        "daily_target": 0,
        "order_type": "MARKET",  # backtest fills at the quoted price regardless; MARKET vs LIMIT is moot here
    }

    result = run_backtest(
        _STRATEGIES[args.strategy], params,
        underlying=args.underlying, lots=args.lots, start=args.start, end=args.end, data_root=DATA_ROOT,
    )

    print(f"\n=== {args.strategy} on {args.underlying}, {args.start} -> {args.end} ===")
    print(f"Params: base_distance={args.base_distance}, lots={args.lots}")
    print(f"Trades: {len(result.trades)}")
    if result.trades:
        print(f"Win rate: {result.win_rate:.1f}%")
        print(f"Total P&L (net of costs): Rs {result.total_pnl:,.0f}")
        print(f"Total costs: Rs {sum(t.costs for t in result.trades):,.0f}")
        print(f"Max drawdown: Rs {result.max_drawdown:,.0f}")
        print(f"Avg P&L/trade: Rs {result.total_pnl / len(result.trades):,.0f}")
        print("\nFirst 5 trades:")
        for t in result.trades[:5]:
            print(f"  {t.opened_at.date()} -> {t.closed_at.date()} | {t.reason} | Rs {t.realized_pnl:,.0f} (legs opened: {t.legs_opened})")
        print("\nLast 5 trades:")
        for t in result.trades[-5:]:
            print(f"  {t.opened_at.date()} -> {t.closed_at.date()} | {t.reason} | Rs {t.realized_pnl:,.0f} (legs opened: {t.legs_opened})")
    else:
        print("No trades were entered in this range -- check params/date range/data availability.")


if __name__ == "__main__":
    main()
