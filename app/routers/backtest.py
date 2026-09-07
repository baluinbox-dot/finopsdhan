"""In-app Backtest feature (Phase C): pick a strategy + underlying + date
range, run it against the locally downloaded historical cache, see the
result on a status page. The actual run happens in a detached OS
subprocess (scripts/backtest/run_single_backtest.py) -- never inside this
uvicorn process, since loading a whole underlying's CSV cache is a
few-hundred-MB operation and this app's deploy VM has a documented history
of near-OOM incidents.

Two safeguards, both enforced here (not in the subprocess, which by the
time it runs has already committed to running):
  - Global one-at-a-time lock: only one backtest_runs row may be
    queued/running across the whole app at once, regardless of who asked --
    the memory risk is shared VM-wide, not per-user.
  - Per-user cooldown: a minimum gap between one user's own submissions
    (backtest_min_interval_minutes), mostly insurance against an accidental
    double-submit rather than a real abuse concern.
A stuck queued/running row past backtest_stale_running_minutes is treated
as abandoned and auto-failed before either check runs -- the OS OOM-killer
can SIGKILL the subprocess outright, bypassing every in-process try/except
it could otherwise rely on to release the lock itself."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.models import BacktestRun, Strategy, UserRole, UserStrategy
from app.strategies.registry import BACKTEST_READY_STRATEGIES, get_strategy_class
from app.templating import flash, render, url

router = APIRouter(prefix="/backtest", tags=["backtest"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data" / "backtest"
LOG_DIR = REPO_ROOT / "data" / "backtest_run_logs"
RUN_SCRIPT = REPO_ROOT / "scripts" / "backtest" / "run_single_backtest.py"

UNDERLYING_CHOICES = ["NIFTY", "BANKNIFTY", "SENSEX"]

# Same fixed backfill start the downloader itself uses (see
# scripts/backtest/download_historical_data.py's _BACKFILL_START) -- the
# earliest date any local data could possibly exist.
EARLIEST_DATA_DATE = date(2024, 9, 4)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite (used in tests, unlike this app's real Postgres deploy) has
    no real timezone-aware column type -- a DateTime(timezone=True) value
    can round-trip back naive. Same guard app.routers.auth and
    app.templating.to_ist already use before doing arithmetic on a
    DB-loaded datetime."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _load_strategy_or_redirect(db: DbSession, request: Request, strategy_id: uuid.UUID):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return None, RedirectResponse(url("/strategies"), status_code=303)
    if strategy.code_ref not in BACKTEST_READY_STRATEGIES:
        flash(request, "Backtesting isn't available for this strategy yet.", "error")
        return None, RedirectResponse(url("/strategies"), status_code=303)
    return strategy, None


def _resolve_base_params(db: DbSession, strategy: Strategy, current_user) -> dict:
    """class defaults -> published strategy's own defaults -> the user's
    existing configured instance for it, if any -- same precedence every
    configure-form route in app.routers.strategies already uses, so a
    backtest defaults to "what I'd actually enter live" rather than a
    generic placeholder."""
    try:
        class_defaults = get_strategy_class(strategy.code_ref).default_params
    except ValueError:
        class_defaults = {}
    existing = db.scalars(
        select(UserStrategy).where(UserStrategy.user_id == current_user.id, UserStrategy.strategy_id == strategy.id)
    ).first()
    return {**class_defaults, **strategy.default_params, **(existing.params if existing else {})}


def _active_lock_row(db: DbSession) -> BacktestRun | None:
    """The backtest_runs row (if any) currently holding the global
    one-at-a-time lock -- queued or running. Call _reap_stale_runs first so
    an abandoned row doesn't hold this forever."""
    return db.scalars(
        select(BacktestRun).where(BacktestRun.status.in_(["queued", "running"]))
        .order_by(BacktestRun.created_at.desc())
    ).first()


def _reap_stale_runs(db: DbSession) -> None:
    cutoff = _now() - timedelta(minutes=get_settings().backtest_stale_running_minutes)
    stale = db.scalars(
        select(BacktestRun).where(BacktestRun.status.in_(["queued", "running"]), BacktestRun.created_at < cutoff)
    ).all()
    for run in stale:
        run.status = "failed"
        run.error_message = "Assumed crashed (stuck in progress past the stale-run threshold) -- lock released."
        run.finished_at = _now()
    if stale:
        db.commit()


@router.get("/{strategy_id}")
def backtest_form(request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    base_params = _resolve_base_params(db, strategy, current_user)

    _reap_stale_runs(db)
    lock_row = _active_lock_row(db)

    recent_runs = db.scalars(
        select(BacktestRun).where(BacktestRun.strategy_id == strategy_id, BacktestRun.user_id == current_user.id)
        .order_by(BacktestRun.created_at.desc()).limit(10)
    ).all()

    return render(
        request,
        "backtest/form.html",
        {
            "current_user": current_user,
            "strategy": strategy,
            "underlyings": UNDERLYING_CHOICES,
            "selected_underlying": (base_params.get("underlying") or "NIFTY").upper(),
            "selected_lots": int(base_params.get("lots") or 1),
            "earliest_data_date": EARLIEST_DATA_DATE,
            "today": date.today(),
            "lock_row": lock_row,
            "recent_runs": recent_runs,
        },
    )


@router.post("/{strategy_id}")
def backtest_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    underlying: str = Form(...),
    start_date: date = Form(...),
    end_date: date = Form(...),
    lots: int = Form(1),
):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    back = RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)
    underlying = underlying.upper()
    if underlying not in UNDERLYING_CHOICES:
        flash(request, "Unknown underlying.", "error")
        return back
    if not (DATA_ROOT / underlying).exists():
        flash(request, f"No local historical data downloaded for {underlying} yet.", "error")
        return back
    if start_date >= end_date:
        flash(request, "Start date must be before end date.", "error")
        return back
    if start_date < EARLIEST_DATA_DATE:
        flash(request, f"No data goes back before {EARLIEST_DATA_DATE.isoformat()}.", "error")
        return back
    if lots <= 0:
        flash(request, "Lots must be greater than zero.", "error")
        return back

    _reap_stale_runs(db)
    lock_row = _active_lock_row(db)
    if lock_row is not None:
        flash(
            request,
            f"A backtest is already {lock_row.status} (queued {lock_row.created_at:%Y-%m-%d %H:%M} UTC) -- "
            "only one can run at a time on this server. Try again once it finishes.",
            "error",
        )
        return back

    cooldown_minutes = get_settings().backtest_min_interval_minutes
    last_own = db.scalars(
        select(BacktestRun).where(BacktestRun.user_id == current_user.id).order_by(BacktestRun.created_at.desc())
    ).first()
    if last_own is not None:
        elapsed = _now() - _aware(last_own.created_at)
        if elapsed < timedelta(minutes=cooldown_minutes):
            wait_minutes = int((timedelta(minutes=cooldown_minutes) - elapsed).total_seconds() // 60) + 1
            flash(request, f"Please wait {wait_minutes} more minute(s) before starting another backtest.", "error")
            return back

    params = _resolve_base_params(db, strategy, current_user)
    params["underlying"] = underlying
    params["lots"] = lots

    run = BacktestRun(
        user_id=current_user.id, strategy_id=strategy_id, underlying=underlying,
        start_date=start_date, end_date=end_date, params=params, status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Close the parent's fd right after Popen returns -- the child has
    # already had it duplicated onto its own stdout by then, so this
    # doesn't affect the child's output, and skipping it would leak one
    # open file handle per submission for the life of this long-running
    # web server process.
    with open(LOG_DIR / f"{run.id}.log", "ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(RUN_SCRIPT), str(run.id)],
            cwd=str(REPO_ROOT), stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # detached -- keeps running independent of this request/worker
        )
    run.pid = proc.pid
    db.commit()

    return RedirectResponse(url(f"/backtest/{strategy_id}/runs/{run.id}"), status_code=303)


@router.get("/{strategy_id}/runs/{run_id}")
def backtest_status(
    request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID, run_id: uuid.UUID,
):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    run = db.get(BacktestRun, run_id)
    is_owner_or_admin = run is not None and (run.user_id == current_user.id or current_user.role == UserRole.SUPERADMIN)
    if run is None or run.strategy_id != strategy_id or not is_owner_or_admin:
        flash(request, "Backtest run not found.", "error")
        return RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)

    equity_curve_json = json.dumps((run.result or {}).get("equity_curve", []))

    return render(
        request,
        "backtest/status.html",
        {
            "current_user": current_user, "strategy": strategy, "run": run,
            "equity_curve_json": equity_curve_json,
        },
    )
