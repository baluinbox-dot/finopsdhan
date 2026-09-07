"""Replays one Strategy class against a HistoricalDataSource, tick by
tick, calling the exact same evaluate_entry/evaluate_exit/evaluate_rolls/
evaluate_leg_exits methods the live engine calls -- so a backtest and a
live run trade off identical decision logic, never a separate
reimplementation that could drift out of sync with a real fix landing in
one place but not the other.

What IS reimplemented here, deliberately: the *orchestration* around
those calls (app.engine.runner's _execute_entry/_close_open_run/
_apply_leg_exits/_apply_rolls), because the real versions are tightly
bound to a SQLAlchemy Session and StrategyRun/Order models. This module
mirrors their control flow and P&L math (leg_pnl computed against the
original entry leg's stored price, exactly like _close_open_run does --
no synthetic "exit leg" gets appended anywhere) using a plain in-memory
Runner instead of the DB, while the parts that actually matter for
correctness (leg_state handling, dedup, P&L itself) are the same shared
pure functions from app.strategies.base (currently_open_legs, leg_pnl),
not reimplemented.

Monkeypatches a target strategy module's own imported names (fetch_chain
_df, fetch_spot_price, fetch_quotes, get_lot_size, _now_ist) for the
duration of the backtest -- the same technique this app's own test suite
already uses (see e.g. tests/test_strategy_dynamic_strangle.py's
_patched_now), just driven by a replay loop instead of one fixed moment.
"""

from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from app.backtest.costs import CostModel
from app.backtest.data_source import DEFAULT_LOT_SIZE, HistoricalDataSource
from app.strategies.base import OrderLeg, Strategy, StrategyContext, currently_open_legs, leg_pnl

IST = ZoneInfo("Asia/Kolkata")

_EMPTY_CHAIN_COLUMNS = ["strike", "ce_security_id", "ce_ltp", "pe_security_id", "pe_ltp"]


@dataclass
class Trade:
    """One completed round-trip (a full entry-to-close cycle, which may
    span several days and several rolls/leg-exits in between)."""
    opened_at: datetime
    closed_at: datetime
    reason: str
    realized_pnl: float  # net of costs
    costs: float
    legs_opened: int  # total legs ever opened this trade, including rolls/scale-ins


@dataclass
class BacktestResult:
    underlying: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)  # (ts, cumulative realized+unrealized)

    @property
    def total_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trades)

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return sum(1 for t in self.trades if t.realized_pnl > 0) / len(self.trades) * 100

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough drop in cumulative equity, in rupees
        (a positive number -- the size of the worst drawdown)."""
        peak = float("-inf")
        worst = 0.0
        for _, pnl in self.equity_curve:
            peak = max(peak, pnl)
            worst = min(worst, pnl - peak)
        return -worst


def _opposite(transaction_type: str) -> str:
    return "BUY" if transaction_type == "SELL" else "SELL"


class _Runner:
    """Holds the in-memory position state a real StrategyRun would hold
    in the DB, for one backtest's replay loop."""

    def __init__(self, ds: HistoricalDataSource, cost_model: CostModel):
        self.ds = ds
        self.costs = cost_model
        self.legs: list[dict[str, Any]] = []
        self.leg_state: dict[str, dict[str, Any]] = {}
        self.entry_premium = 0.0
        self.realized_pnl_so_far = 0.0
        self.total_costs_so_far = 0.0
        self.legs_opened_count = 0
        self.opened_at: datetime | None = None
        self.today_run_count = 0
        self.current_date: date | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def notes(self) -> dict[str, Any]:
        return {
            "legs": self.legs, "leg_state": self.leg_state,
            "entry_premium": self.entry_premium, "realized_pnl_so_far": self.realized_pnl_so_far,
        }

    def _record_entry_leg(self, leg: OrderLeg) -> None:
        fill_price = self.costs.fill_price(leg.price, leg.transaction_type)
        self.total_costs_so_far += self.costs.order_cost(fill_price, leg.quantity, leg.transaction_type)
        data = asdict(leg)
        data["price"] = fill_price
        self.legs.append(data)
        self.legs_opened_count += 1

    def _close_leg(self, ts: int, leg_data: dict[str, Any]) -> bool:
        """Close one currently-open leg at a fresh simulated quote,
        exactly mirroring app.engine.runner._close_open_run's math
        (leg_pnl against the *original* entry price, never a synthetic
        appended "exit leg"). Returns False (leaves the leg open, same as
        a live failed quote fetch) if no price is available right now."""
        quote = self.ds.quote_for(leg_data["security_id"], ts)
        if quote is None:
            return False
        exit_side = _opposite(leg_data["transaction_type"])
        fill_price = self.costs.fill_price(quote, exit_side)
        self.total_costs_so_far += self.costs.order_cost(fill_price, leg_data["quantity"], exit_side)
        self.realized_pnl_so_far += leg_pnl(leg_data, fill_price)
        self.leg_state[str(leg_data["security_id"])] = {"status": "closed"}
        return True

    def start_run(self, now_ist: datetime, legs: list[OrderLeg]) -> None:
        self.legs = []
        self.leg_state = {}
        self.total_costs_so_far = 0.0
        self.realized_pnl_so_far = 0.0
        self.legs_opened_count = 0
        for leg in legs:
            self._record_entry_leg(leg)
        self.entry_premium = sum(l.price for l in legs if l.transaction_type == "SELL") - sum(
            l.price for l in legs if l.transaction_type == "BUY"
        )
        self.opened_at = now_ist
        self.today_run_count += 1

    def close_all(self, ts: int) -> bool:
        """All-or-nothing: only actually closes anything if every
        currently-open leg can be priced this tick, same as the live
        engine's _close_open_run."""
        open_legs = currently_open_legs(self.legs, self.leg_state)
        if any(self.ds.quote_for(l["security_id"], ts) is None for l in open_legs):
            return False
        for leg_data in open_legs:
            self._close_leg(ts, leg_data)
        return True

    def apply_leg_exits(self, ts: int, decision: dict[str, Any]) -> None:
        close_ids = {str(sid) for sid in (decision.get("close_security_ids") or [])}
        to_close = [l for l in currently_open_legs(self.legs, self.leg_state) if str(l["security_id"]) in close_ids]
        if to_close and any(self.ds.quote_for(l["security_id"], ts) is None for l in to_close):
            return  # can't price every named leg this pass -- skip, retry next tick
        for leg_data in to_close:
            self._close_leg(ts, leg_data)
        for sid, patch_ in (decision.get("leg_state_patch") or {}).items():
            sid = str(sid)
            self.leg_state[sid] = {**(self.leg_state.get(sid) or {"status": "open"}), **patch_}

    def apply_rolls(self, ts: int, decision: dict[str, Any]) -> None:
        """`decision` is `{"rolls": [roll, ...]}` -- a strategy can name
        more than one independent roll in a single pass (e.g. Iron Condor
        Rolling adjusting both sides at once); each is applied in order,
        with `currently_open_legs` recomputed fresh every iteration so a
        later roll in the same pass sees the previous one's result, same
        as app.engine.runner._apply_rolls."""
        for roll in decision.get("rolls") or []:
            close_ids = {str(sid) for sid in (roll.get("close_security_ids") or [])}
            to_close = [l for l in currently_open_legs(self.legs, self.leg_state) if str(l["security_id"]) in close_ids]
            if close_ids and (len(to_close) != len(close_ids) or any(self.ds.quote_for(l["security_id"], ts) is None for l in to_close)):
                continue  # a named leg isn't open, or can't be priced -- don't guess, skip just this roll
            for leg_data in to_close:
                self._close_leg(ts, leg_data)
            for new_leg in roll.get("new_legs") or []:
                self._record_entry_leg(new_leg)
            for sid, patch_ in (roll.get("leg_state_patch") or {}).items():
                sid = str(sid)
                self.leg_state[sid] = {**(self.leg_state.get(sid) or {"status": "open"}), **patch_}

    def is_fully_closed(self) -> bool:
        primary_ids = {str(l["security_id"]) for l in self.legs if l.get("role") == "primary"}
        if not primary_ids:
            return True
        return all(self.leg_state.get(sid, {"status": "open"})["status"] == "closed" for sid in primary_ids)

    def finish(self, closed_at: datetime, reason: str) -> Trade:
        trade = Trade(
            opened_at=self.opened_at, closed_at=closed_at, reason=reason,
            realized_pnl=self.realized_pnl_so_far - self.total_costs_so_far,
            costs=self.total_costs_so_far, legs_opened=self.legs_opened_count,
        )
        self.opened_at = None
        return trade


def _tick(runner: _Runner, ts: int, now_ist: datetime, impl: Strategy, params: dict[str, Any], result: BacktestResult) -> None:
    today = now_ist.date()
    if today != runner.current_date:
        runner.current_date = today
        runner.today_run_count = 0

    if not runner.is_open:
        ctx = StrategyContext(dhan_client=None, params=params, today_run_count=runner.today_run_count)
        legs = impl.evaluate_entry(ctx)
        if legs:
            runner.start_run(now_ist, legs)
    else:
        ctx = StrategyContext(dhan_client=None, params=params)
        notes = runner.notes()
        if impl.evaluate_exit(ctx, notes):
            if runner.close_all(ts):
                result.trades.append(runner.finish(now_ist, "evaluate_exit"))
        else:
            leg_decision = impl.evaluate_leg_exits(ctx, notes)
            if leg_decision:
                runner.apply_leg_exits(ts, leg_decision)
                if runner.is_fully_closed():
                    result.trades.append(runner.finish(now_ist, "evaluate_leg_exits"))
            if runner.is_open:
                roll_decision = impl.evaluate_rolls(ctx, runner.notes())
                if roll_decision:
                    runner.apply_rolls(ts, roll_decision)

    realized_so_far = sum(t.realized_pnl for t in result.trades)
    unrealized = 0.0
    if runner.is_open:
        for leg in currently_open_legs(runner.legs, runner.leg_state):
            price = runner.ds.quote_for(leg["security_id"], ts)
            if price is not None:
                unrealized += leg_pnl(leg, price)
    result.equity_curve.append((now_ist, realized_so_far + unrealized))


def _quotes_compat(ds: HistoricalDataSource, securities: dict[str, list], ts: int) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for segment, sids in securities.items():
        for sid in sids:
            price = ds.quote_for(str(sid), ts)
            if price is not None:
                out[(segment, str(sid))] = {"last_price": price}
    return out


def run_backtest(
    strategy_cls: type[Strategy],
    params: dict[str, Any],
    *,
    underlying: str,
    lots: int = 1,
    start: date,
    end: date,
    data_root: Path | str,
    cost_model: CostModel | None = None,
) -> BacktestResult:
    """Replay `strategy_cls` (with `params`) against the local historical
    cache for `underlying`, from `start` to `end` (inclusive), at the
    data's own 5-minute granularity. Returns every completed trade plus
    an equity curve. Raises ValueError if there's no downloaded data at
    all in that range -- a setup problem the caller should see, not
    silently produce an empty result for."""
    ds = HistoricalDataSource(underlying, data_root)
    cost_model = cost_model or CostModel()
    lot_size = DEFAULT_LOT_SIZE.get(underlying.upper(), 75) * lots

    module = importlib.import_module(strategy_cls.__module__)
    current_ts: list[int] = [0]
    current_now: list[datetime] = [datetime.now(IST)]

    def _chain(*_a: Any, **_k: Any) -> tuple[pd.DataFrame, float]:
        got = ds.chain_at(current_ts[0])
        return got if got is not None else (pd.DataFrame(columns=_EMPTY_CHAIN_COLUMNS), 0.0)

    def _spot(*_a: Any, **_k: Any) -> float | None:
        return ds.spot_at(current_ts[0])

    def _quotes(_dhan_client: Any, securities: dict[str, list]) -> dict[tuple[str, str], dict[str, Any]]:
        return _quotes_compat(ds, securities, current_ts[0])

    def _now() -> datetime:
        return current_now[0]

    patchers = []
    if hasattr(module, "fetch_chain_df"):
        patchers.append(patch.object(module, "fetch_chain_df", side_effect=_chain))
    if hasattr(module, "fetch_spot_price"):
        patchers.append(patch.object(module, "fetch_spot_price", side_effect=_spot))
    if hasattr(module, "fetch_quotes"):
        patchers.append(patch.object(module, "fetch_quotes", side_effect=_quotes))
    if hasattr(module, "get_lot_size"):
        patchers.append(patch.object(module, "get_lot_size", return_value=lot_size))
    if hasattr(module, "_now_ist"):
        patchers.append(patch.object(module, "_now_ist", side_effect=_now))

    all_ts = ds.timestamps()
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=IST).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=IST).timestamp())
    ticks = [int(t) for t in all_ts if start_ts <= t <= end_ts]
    if not ticks:
        raise ValueError(f"No {underlying} data in range {start}..{end} under {data_root}")

    result = BacktestResult(underlying=underlying.upper())
    impl = strategy_cls()
    runner = _Runner(ds, cost_model)

    for p in patchers:
        p.start()
    try:
        for ts in ticks:
            current_ts[0] = ts
            current_now[0] = datetime.fromtimestamp(ts, tz=IST)
            _tick(runner, ts, current_now[0], impl, params, result)
    finally:
        for p in patchers:
            p.stop()

    return result
