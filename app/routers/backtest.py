"""In-app Backtest feature (Phase C, + parameter sweeps): pick a strategy +
underlying + date range, run it against the locally downloaded historical
cache, see the result on a status page. The actual run happens in a
detached OS subprocess (scripts/backtest/run_single_backtest.py) -- never
inside this uvicorn process, since loading a whole underlying's CSV cache
is a few-hundred-MB operation and this app's deploy VM has a documented
history of near-OOM incidents.

Three safeguards, all enforced here (not in the subprocess, which by the
time it runs has already committed to running):
  - Global one-at-a-time lock: only one backtest_runs row may be
    *dispatched* (see below) across the whole app at once, regardless of
    who asked -- the memory risk is shared VM-wide, not per-user.
  - Per-user cooldown: a minimum gap between one user's own submissions
    (backtest_min_interval_minutes), mostly insurance against an accidental
    double-submit rather than a real abuse concern.
  - A hard cap (_MAX_SWEEP_VALUES) on how many values one sweep can queue,
    since each is a full subprocess run and they queue up serially behind
    the one-at-a-time lock.
A stuck dispatched row past backtest_stale_running_minutes is treated as
abandoned and auto-failed before any of the above runs -- the OS
OOM-killer can SIGKILL the subprocess outright, bypassing every in-process
try/except that could otherwise release the lock itself.

Sweeps: submitting with a non-empty sweep_param creates several
backtest_runs rows (one per value, sharing one sweep_id) instead of one.
Only the first is *dispatched* immediately (subprocess spawned, pid set);
the rest sit "queued" with no pid at all -- genuinely waiting their turn,
not a transient state, and NEVER reaped as stale (see BacktestRun's own
docstring for the pid-based distinction this whole module leans on).
app.engine.scheduler's periodic queue-advance job calls
advance_backtest_queue() below, which dispatches the next undispatched row
once the lock frees up -- this is the only mechanism that makes a sweep's
2nd..Nth member ever actually run."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select

from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.models import BacktestRun, Strategy, UserRole, UserStrategy
from app.strategies.registry import BACKTEST_READY_STRATEGIES, get_strategy_class
from app.templating import flash, render, url

router = APIRouter(prefix="/backtest", tags=["backtest"])

# A sweep queues this many full subprocess runs serially behind the
# one-at-a-time lock -- capped so a single submission can't monopolize the
# app's one backtest slot for an unbounded stretch (each run takes seconds
# to low minutes against the data sizes seen so far, so even the cap is
# comfortably a few minutes worst-case, not hours).
_MAX_SWEEP_VALUES = 10

# Params never offered for sweeping even though they're numeric --
# already their own dedicated form fields (underlying/date range aren't
# numeric anyway), or sweeping them wouldn't mean what a user expects.
_NON_SWEEPABLE_PARAM_KEYS = {"underlying", "lots"}

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data" / "backtest"
LOG_DIR = REPO_ROOT / "data" / "backtest_run_logs"
RUN_SCRIPT = REPO_ROOT / "scripts" / "backtest" / "run_single_backtest.py"

UNDERLYING_CHOICES = ["NIFTY", "BANKNIFTY", "SENSEX"]

# Same fixed backfill start the downloader itself uses (see
# scripts/backtest/download_historical_data.py's _BACKFILL_START) -- the
# earliest date any local data could possibly exist. Keep in sync with
# that constant -- moved back to 2024-01-01 on Balu's request 2026-09-15.
EARLIEST_DATA_DATE = date(2024, 1, 1)


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


def _resolve_instance(db: DbSession, strategy: Strategy, current_user, user_strategy_id: str) -> UserStrategy | None:
    """The specific instance named by user_strategy_id, if it's valid and
    owned by this user -- or, when none was named, an arbitrary one of this
    user's instances of this strategy (a user can run several concurrently,
    e.g. a CE seller and a PE seller, so "arbitrary" is a real ambiguity;
    always pass user_strategy_id when the caller knows which one it means,
    e.g. arriving from that instance's own "Save & Backtest" button)."""
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is not None and existing.user_id == current_user.id and existing.strategy_id == strategy.id:
            return existing
        return None
    return db.scalars(
        select(UserStrategy).where(UserStrategy.user_id == current_user.id, UserStrategy.strategy_id == strategy.id)
    ).first()


def _resolve_base_params(db: DbSession, strategy: Strategy, current_user, user_strategy_id: str = "") -> dict:
    """class defaults -> published strategy's own defaults -> the resolved
    instance's params, if any -- same precedence every configure-form route
    in app.routers.strategies already uses, so a backtest defaults to
    "what I'd actually enter live" rather than a generic placeholder."""
    try:
        class_defaults = get_strategy_class(strategy.code_ref).default_params
    except ValueError:
        class_defaults = {}
    existing = _resolve_instance(db, strategy, current_user, user_strategy_id)
    return {**class_defaults, **strategy.default_params, **(existing.params if existing else {})}


def _sweepable_params(base_params: dict) -> list[str]:
    """Top-level keys of base_params whose value is a plain number -- the
    only shape a sweep can vary (a nested dict, e.g. Single-Leg Seller's
    stop_loss/target sub-objects, isn't sweepable in v1). bool is
    deliberately excluded even though it's technically an int subclass in
    Python -- sweeping "hedge_enabled" across [0, 1] would be a confusing
    way to spell True/False."""
    return sorted(
        k for k, v in base_params.items()
        if k not in _NON_SWEEPABLE_PARAM_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
    )


def _dispatched_filter():
    """A row counts toward the one-at-a-time lock (and the stale-reap
    check) only once it's actually dispatched -- status=="running", or
    status=="queued" with a pid already assigned (a lone run or a sweep's
    first member, in the brief window before its own subprocess flips it
    to "running"). A sweep's later, undispatched members (status=="queued",
    pid IS NULL) are a real wait queue, not a stuck/abandoned state --
    see BacktestRun's own docstring."""
    return or_(BacktestRun.status == "running", (BacktestRun.status == "queued") & BacktestRun.pid.is_not(None))


def _active_lock_row(db: DbSession) -> BacktestRun | None:
    """The backtest_runs row (if any) currently holding the global
    one-at-a-time lock. Call _reap_stale_runs first so an abandoned row
    doesn't hold this forever."""
    return db.scalars(select(BacktestRun).where(_dispatched_filter()).order_by(BacktestRun.created_at.desc())).first()


def _next_undispatched_run(db: DbSession) -> BacktestRun | None:
    """The oldest sweep member still waiting for a subprocess (status ==
    "queued", pid IS NULL) -- what advance_backtest_queue dispatches next
    once the lock frees up."""
    return db.scalars(
        select(BacktestRun).where(BacktestRun.status == "queued", BacktestRun.pid.is_(None))
        .order_by(BacktestRun.created_at.asc())
    ).first()


def _reap_stale_runs(db: DbSession) -> None:
    cutoff = _now() - timedelta(minutes=get_settings().backtest_stale_running_minutes)
    stale = db.scalars(select(BacktestRun).where(_dispatched_filter(), BacktestRun.created_at < cutoff)).all()
    for run in stale:
        run.status = "failed"
        run.error_message = "Assumed crashed (stuck in progress past the stale-run threshold) -- lock released."
        run.finished_at = _now()
    if stale:
        db.commit()


def _spawn_subprocess(db: DbSession, run: BacktestRun) -> None:
    """Actually dispatch one backtest_runs row: spawn its detached
    subprocess and record the pid. Shared by backtest_submit (dispatches a
    lone run, or a sweep's first member, immediately inline) and
    advance_backtest_queue (dispatches a sweep's later members once the
    lock frees up)."""
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


def advance_backtest_queue(db: DbSession) -> None:
    """Called periodically by app.engine.scheduler -- dispatches the next
    undispatched sweep member once the lock is free. A lone run or a
    sweep's first member is always dispatched immediately inline in
    backtest_submit, so in practice this only ever matters for sweep
    continuations (a sweep's 2nd..Nth value)."""
    _reap_stale_runs(db)
    if _active_lock_row(db) is not None:
        return
    next_run = _next_undispatched_run(db)
    if next_run is None:
        return
    _spawn_subprocess(db, next_run)


def _recent_sweeps(db: DbSession, strategy_id: uuid.UUID, user_id: uuid.UUID, limit: int = 5) -> list[dict]:
    """One summary entry per recent sweep (grouped from its member rows in
    Python -- there are never more than _MAX_SWEEP_VALUES of them, so this
    is cheap), newest first, with an aggregate status a viewer can scan at
    a glance without opening the sweep."""
    members = db.scalars(
        select(BacktestRun).where(
            BacktestRun.strategy_id == strategy_id, BacktestRun.user_id == user_id, BacktestRun.sweep_id.is_not(None),
        ).order_by(BacktestRun.created_at.desc()).limit(limit * _MAX_SWEEP_VALUES)
    ).all()
    grouped: dict[uuid.UUID, list[BacktestRun]] = {}
    for m in members:
        grouped.setdefault(m.sweep_id, []).append(m)

    summaries = []
    for sweep_id, rows in grouped.items():
        statuses = {r.status for r in rows}
        if statuses == {"completed"}:
            agg = "completed"
        elif "running" in statuses or ("queued" in statuses and rows[0].pid is not None):
            agg = "running"
        elif statuses <= {"queued"}:
            agg = "queued"
        elif "completed" in statuses:
            agg = "partial"
        else:
            agg = "failed"
        summaries.append({
            "sweep_id": sweep_id, "sweep_param": rows[0].sweep_param,
            "created_at": max(r.created_at for r in rows), "count": len(rows), "status": agg,
        })
    summaries.sort(key=lambda s: s["created_at"], reverse=True)
    return summaries[:limit]


@router.get("/{strategy_id}")
def backtest_form(
    request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID, user_strategy_id: str = "",
):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    instance = _resolve_instance(db, strategy, current_user, user_strategy_id)
    if user_strategy_id and instance is None:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)
    base_params = _resolve_base_params(db, strategy, current_user, user_strategy_id)

    _reap_stale_runs(db)
    lock_row = _active_lock_row(db)

    recent_runs = db.scalars(
        select(BacktestRun).where(
            BacktestRun.strategy_id == strategy_id, BacktestRun.user_id == current_user.id,
            BacktestRun.sweep_id.is_(None),
        ).order_by(BacktestRun.created_at.desc()).limit(10)
    ).all()
    recent_sweeps = _recent_sweeps(db, strategy_id, current_user.id)

    return render(
        request,
        "backtest/form.html",
        {
            "current_user": current_user,
            "strategy": strategy,
            "instance": instance,
            "user_strategy_id": str(instance.id) if instance else "",
            "underlyings": UNDERLYING_CHOICES,
            "selected_underlying": (base_params.get("underlying") or "NIFTY").upper(),
            "selected_lots": int(base_params.get("lots") or 1),
            "earliest_data_date": EARLIEST_DATA_DATE,
            "today": date.today(),
            "lock_row": lock_row,
            "recent_runs": recent_runs,
            "recent_sweeps": recent_sweeps,
            "sweepable_params": _sweepable_params(base_params),
            "max_sweep_values": _MAX_SWEEP_VALUES,
        },
    )


def _parse_sweep_values(raw: str) -> list[float] | str:
    """Comma-separated numbers -> a deduped, order-preserving list of
    floats, or an error message string (never both) -- 2..._MAX_SWEEP_VALUES
    of them, since a sweep of 0 or 1 value is just an ordinary run."""
    pieces = [p.strip() for p in raw.split(",") if p.strip()]
    values: list[float] = []
    for p in pieces:
        try:
            v = float(p)
        except ValueError:
            return f"'{p}' isn't a number."
        if v not in values:
            values.append(v)
    if len(values) < 2:
        return "Enter at least 2 different values, separated by commas, to sweep a parameter."
    if len(values) > _MAX_SWEEP_VALUES:
        return f"A sweep can compare at most {_MAX_SWEEP_VALUES} values at once (got {len(values)})."
    return values


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
    user_strategy_id: str = Form(""),
    sweep_param: str = Form(""),
    sweep_values: str = Form(""),
):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    back_url = f"/backtest/{strategy_id}"
    if user_strategy_id:
        back_url += f"?user_strategy_id={user_strategy_id}"
    back = RedirectResponse(url(back_url), status_code=303)
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

    params = _resolve_base_params(db, strategy, current_user, user_strategy_id)
    sweep_values_list: list[float] | None = None
    if sweep_param:
        if sweep_param not in _sweepable_params(params):
            flash(request, "That parameter can't be swept for this strategy.", "error")
            return back
        parsed = _parse_sweep_values(sweep_values)
        if isinstance(parsed, str):
            flash(request, parsed, "error")
            return back
        sweep_values_list = parsed

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

    params["underlying"] = underlying
    params["lots"] = lots

    if sweep_values_list is None:
        run = BacktestRun(
            user_id=current_user.id, strategy_id=strategy_id, underlying=underlying,
            start_date=start_date, end_date=end_date, params=params, status="queued",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        _spawn_subprocess(db, run)
        return RedirectResponse(url(f"/backtest/{strategy_id}/runs/{run.id}"), status_code=303)

    # Sweep: one row per value, all sharing one sweep_id -- only the first
    # is dispatched now; advance_backtest_queue (app.engine.scheduler)
    # picks up the rest as the lock frees up, one at a time.
    sweep_id = uuid.uuid4()
    first_run: BacktestRun | None = None
    for value in sweep_values_list:
        run_params = dict(params)
        run_params[sweep_param] = value
        run = BacktestRun(
            user_id=current_user.id, strategy_id=strategy_id, underlying=underlying,
            start_date=start_date, end_date=end_date, params=run_params, status="queued",
            sweep_id=sweep_id, sweep_param=sweep_param, sweep_value=value,
        )
        db.add(run)
        if first_run is None:
            first_run = run
    db.commit()
    db.refresh(first_run)
    _spawn_subprocess(db, first_run)

    return RedirectResponse(url(f"/backtest/{strategy_id}/sweeps/{sweep_id}"), status_code=303)


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


@router.post("/{strategy_id}/runs/{run_id}/delete")
def delete_backtest_run(
    request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID, run_id: uuid.UUID,
):
    """Permanently remove one saved backtest run. Owner-only (no admin
    override, unlike the view route) -- matches delete_instance/delete_note.
    Blocked while still queued/running: those rows are tracked by
    pid/lock bookkeeping the scheduler depends on (_reap_stale_runs /
    advance_backtest_queue) and deleting one out from under that would
    leave a dangling subprocess or a stuck lock."""
    run = db.get(BacktestRun, run_id)
    if run is None or run.strategy_id != strategy_id or run.user_id != current_user.id:
        flash(request, "Backtest run not found.", "error")
        return RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)

    if run.status in ("queued", "running"):
        flash(request, "Can't delete a backtest that's still queued or running — wait for it to finish.", "error")
        return RedirectResponse(url(f"/backtest/{strategy_id}/runs/{run_id}"), status_code=303)

    db.delete(run)
    db.commit()
    flash(request, "Backtest run deleted.", "success")
    return RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)


@router.get("/{strategy_id}/sweeps/{sweep_id}")
def backtest_sweep_status(
    request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID, sweep_id: uuid.UUID,
):
    strategy, redirect = _load_strategy_or_redirect(db, request, strategy_id)
    if redirect:
        return redirect

    runs = db.scalars(
        select(BacktestRun).where(BacktestRun.sweep_id == sweep_id, BacktestRun.strategy_id == strategy_id)
        .order_by(BacktestRun.sweep_value.asc())
    ).all()
    is_owner_or_admin = bool(runs) and (runs[0].user_id == current_user.id or current_user.role == UserRole.SUPERADMIN)
    if not runs or not is_owner_or_admin:
        flash(request, "Sweep not found.", "error")
        return RedirectResponse(url(f"/backtest/{strategy_id}"), status_code=303)

    all_done = all(r.status in ("completed", "failed") for r in runs)
    completed = [r for r in runs if r.status == "completed"]
    headline = max(completed, key=lambda r: (r.result or {}).get("total_pnl", float("-inf"))) if completed else None

    chart_data = json.dumps([
        {
            "value": float(r.sweep_value), "status": r.status,
            "total_pnl": (r.result or {}).get("total_pnl") if r.status == "completed" else None,
        }
        for r in runs
    ])

    return render(
        request,
        "backtest/sweep_status.html",
        {
            "current_user": current_user, "strategy": strategy, "runs": runs,
            "sweep_id": sweep_id, "sweep_param": runs[0].sweep_param,
            "all_done": all_done, "headline": headline, "chart_data": chart_data,
        },
    )
