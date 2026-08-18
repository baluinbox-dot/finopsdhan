"""Realized P&L reporting: date-wise and per-instance breakdown, plus a
trade-level detail list (entry/exit time, price context, and why each
position closed) — so a user can evaluate paper-trading results before
committing a strategy to live mode."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import CurrentUser, DbSession
from app.models import StrategyRun, UserStrategy
from app.templating import render, to_ist

router = APIRouter(prefix="/reports", tags=["reports"])

IST = ZoneInfo("Asia/Kolkata")


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_ist_date(dt: datetime | None) -> date:
    if dt is None:
        return date.min
    return to_ist(dt).date()


@router.get("")
def pnl_report(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    instance: str = "",
    mode: str = "",
    start: str = "",
    end: str = "",
):
    user_strategies = db.scalars(
        select(UserStrategy)
        .where(UserStrategy.user_id == current_user.id)
        .options(selectinload(UserStrategy.strategy))
    ).all()

    runs = db.scalars(
        select(StrategyRun)
        .join(UserStrategy, StrategyRun.user_strategy_id == UserStrategy.id)
        .where(UserStrategy.user_id == current_user.id, StrategyRun.status == "closed")
        .options(selectinload(StrategyRun.user_strategy).selectinload(UserStrategy.strategy))
        .order_by(StrategyRun.closed_at.desc())
    ).all()

    # Filters applied in Python, not SQL — row counts here are small (this
    # is per-user trade history, not a bulk analytics table), and it keeps
    # the date-bucketing (IST calendar day, not UTC) consistent with how
    # it's computed below rather than needing DB-specific date functions.
    start_date = _parse_date(start)
    end_date = _parse_date(end)

    filtered: list[StrategyRun] = []
    for run in runs:
        if instance and str(run.user_strategy_id) != instance:
            continue
        if mode and run.user_strategy.mode.value != mode:
            continue
        run_date = _to_ist_date(run.closed_at)
        if start_date and run_date < start_date:
            continue
        if end_date and run_date > end_date:
            continue
        filtered.append(run)

    # Date x instance aggregation for the summary table.
    buckets: dict[tuple[date, str], dict] = {}
    for run in filtered:
        run_date = _to_ist_date(run.closed_at)
        key = (run_date, str(run.user_strategy_id))
        bucket = buckets.setdefault(
            key,
            {
                "date": run_date,
                "label": run.user_strategy.label or run.user_strategy.strategy.name,
                "mode": run.user_strategy.mode.value,
                "trades": 0,
                "pnl": 0.0,
                "wins": 0,
                "losses": 0,
            },
        )
        pnl = float(run.realized_pnl or 0)
        bucket["trades"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

    date_rows = sorted(buckets.values(), key=lambda r: (r["date"], r["label"]), reverse=True)

    total_pnl = sum(float(r.realized_pnl or 0) for r in filtered)
    total_trades = len(filtered)
    wins = sum(1 for r in filtered if float(r.realized_pnl or 0) > 0)
    losses = sum(1 for r in filtered if float(r.realized_pnl or 0) < 0)
    win_rate = (wins / total_trades * 100) if total_trades else None
    avg_pnl = (total_pnl / total_trades) if total_trades else None

    by_day_totals: dict[date, float] = defaultdict(float)
    for run in filtered:
        by_day_totals[_to_ist_date(run.closed_at)] += float(run.realized_pnl or 0)
    best_day = max(by_day_totals.items(), key=lambda kv: kv[1], default=None)
    worst_day = min(by_day_totals.items(), key=lambda kv: kv[1], default=None)

    return render(
        request,
        "reports/pnl.html",
        {
            "current_user": current_user,
            "user_strategies": user_strategies,
            "selected_instance": instance,
            "selected_mode": mode,
            "start": start,
            "end": end,
            "date_rows": date_rows,
            "trades": filtered,
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "best_day": best_day,
            "worst_day": worst_day,
        },
    )
