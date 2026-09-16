"""Superadmin: review and approve new accounts, and trigger/monitor the
historical-data-download maintenance script.

A registered, email-verified account still can't log in until the
superadmin approves it here (see app.routers.auth.login_submit). "Disable"
reuses the existing `is_active` flag (already checked at login), rather than
adding a separate rejection state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.deps import DbSession, SuperadminUser
from app.models import DataDownloadRun, User
from app.routers.auth import _send_approved_email
from app.templating import flash, render, url

router = APIRouter(prefix="/admin", tags=["admin"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_SCRIPT = REPO_ROOT / "scripts" / "backtest" / "download_historical_data.py"
DOWNLOAD_LOG_DIR = REPO_ROOT / "data" / "download_run_logs"
_DOWNLOAD_LOG_TAIL_LINES = 200


@router.get("/users")
def list_users(request: Request, db: DbSession, current_user: SuperadminUser):
    all_users = db.scalars(select(User).order_by(User.is_approved.asc(), User.created_at.desc())).all()
    return render(
        request,
        "admin/users.html",
        {
            "current_user": current_user,
            "users": all_users,
            "pending_count": sum(1 for u in all_users if not u.is_approved and u.email_verified),
        },
    )


@router.post("/users/{user_id}/approve")
def approve_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    if not user.email_verified:
        flash(request, f"{user.email} hasn't verified their email yet — can't approve.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_approved = True
    user.approved_at = datetime.now(timezone.utc)
    db.commit()
    _send_approved_email(user)

    flash(request, f"{user.email} approved — they can now log in.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    if user.id == current_user.id:
        flash(request, "You can't disable your own account.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_active = False
    db.commit()

    flash(request, f"{user.email} disabled — they can no longer log in.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)


@router.post("/users/{user_id}/enable")
def enable_user(request: Request, db: DbSession, current_user: SuperadminUser, user_id: uuid.UUID):
    user = db.get(User, user_id)
    if user is None:
        flash(request, "No such user.", "error")
        return RedirectResponse(url("/admin/users"), status_code=303)

    user.is_active = True
    db.commit()

    flash(request, f"{user.email} re-enabled.", "success")
    return RedirectResponse(url("/admin/users"), status_code=303)


# --- Historical data download (refreshes the shared on-disk backtest cache) ---


def _is_pid_alive(pid: int | None) -> bool:
    """Best-effort liveness check for a detached subprocess's pid --
    reliable on POSIX (the deploy VM, where this matters for real);
    Windows (local dev) can't cheaply check this the same way, so any
    ambiguous result there defaults to "still alive" rather than risk a
    false "not running" that lets a second download start concurrently
    against the same shared Dhan account and on-disk cache."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _active_download(db: DbSession) -> DataDownloadRun | None:
    """The current "running" row, if any -- reaped first if its own pid
    is no longer alive (the OS OOM-killed it, or the VM rebooted, before
    it ever got to write its own "failed" status). Same stale-reap
    philosophy as BacktestRun's one-at-a-time lock in app.routers.backtest."""
    running = db.scalars(
        select(DataDownloadRun).where(DataDownloadRun.status == "running").order_by(DataDownloadRun.created_at.desc())
    ).first()
    if running is None:
        return None
    if not _is_pid_alive(running.pid):
        running.status = "failed"
        running.error_message = "Assumed crashed (process no longer running) -- lock released."
        running.finished_at = datetime.now(timezone.utc)
        db.commit()
        return None
    return running


def _download_log_tail(run: DataDownloadRun | None) -> str:
    if run is None:
        return ""
    log_path = DOWNLOAD_LOG_DIR / f"{run.id}.log"
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-_DOWNLOAD_LOG_TAIL_LINES:])


@router.get("/data-download")
def data_download_page(request: Request, db: DbSession, current_user: SuperadminUser):
    active = _active_download(db)
    recent = db.scalars(select(DataDownloadRun).order_by(DataDownloadRun.created_at.desc()).limit(10)).all()
    log_target = active or (recent[0] if recent else None)
    return render(
        request,
        "admin/data_download.html",
        {
            "current_user": current_user,
            "active": active,
            "recent": recent,
            "log_tail": _download_log_tail(log_target),
        },
    )


@router.post("/data-download/start")
def start_data_download(request: Request, db: DbSession, current_user: SuperadminUser):
    if _active_download(db) is not None:
        flash(request, "A data download is already running.", "error")
        return RedirectResponse(url("/admin/data-download"), status_code=303)

    run = DataDownloadRun(started_by_user_id=current_user.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    DOWNLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Close the parent's fd right after Popen returns -- the child has
    # already had it duplicated onto its own stdout by then (same pattern
    # as app.routers.backtest._spawn_subprocess).
    with open(DOWNLOAD_LOG_DIR / f"{run.id}.log", "ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(DOWNLOAD_SCRIPT), str(run.id)],
            cwd=str(REPO_ROOT), stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # detached -- keeps running independent of this request/worker
        )
    run.pid = proc.pid
    db.commit()

    flash(request, "Historical data download started — this can take several hours.", "success")
    return RedirectResponse(url("/admin/data-download"), status_code=303)
