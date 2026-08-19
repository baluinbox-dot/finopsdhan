"""Dashboard: active strategies, recent orders/paper-fills, basic P&L,
and safety controls (pause-all, kill switch)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.dhan.client import DhanNotConnectedError, get_user_dhan_client
from app.deps import CurrentUser, DbSession
from app.engine.pnl import compute_live_pnl
from app.engine.runner import find_open_run
from app.models import Order, UserStrategy
from app.templating import flash, render, url

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Recent Orders pagination — page-size choices offered in the UI. An
# out-of-list value in the query string (tampered or stale link) falls
# back to the default rather than erroring.
ORDERS_PER_PAGE_CHOICES = (5, 10, 15, 20, 25, 50, 100)
ORDERS_PER_PAGE_DEFAULT = 10


@router.get("")
def dashboard(request: Request, db: DbSession, current_user: CurrentUser, page: int = 1, per_page: int = ORDERS_PER_PAGE_DEFAULT):
    user_strategies = db.scalars(
        select(UserStrategy).where(UserStrategy.user_id == current_user.id)
    ).all()
    active_count = sum(1 for us in user_strategies if us.is_active)
    open_run_ids = {us.id for us in user_strategies if find_open_run(us) is not None}

    if per_page not in ORDERS_PER_PAGE_CHOICES:
        per_page = ORDERS_PER_PAGE_DEFAULT
    page = max(1, page)

    total_orders = db.scalar(select(func.count()).select_from(Order).where(Order.user_id == current_user.id)) or 0
    total_pages = max(1, -(-total_orders // per_page))  # ceil division
    page = min(page, total_pages)

    recent_orders = db.scalars(
        select(Order)
        .where(Order.user_id == current_user.id)
        .order_by(Order.placed_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    # The two stat-card counts ("Paper Orders (recent)" / "Live Orders
    # (recent)") intentionally still summarize a fixed recent window, not
    # the current page — they're headline counts, not tied to whichever
    # page/page-size the Recent Orders table happens to be showing.
    recent_for_counts = db.scalars(
        select(Order).where(Order.user_id == current_user.id).order_by(Order.placed_at.desc()).limit(25)
    ).all()
    paper_count = sum(1 for o in recent_for_counts if o.is_paper)
    live_count = sum(1 for o in recent_for_counts if not o.is_paper)

    return render(
        request,
        "dashboard/index.html",
        {
            "current_user": current_user,
            "user_strategies": user_strategies,
            "active_count": active_count,
            "open_run_ids": open_run_ids,
            "recent_orders": recent_orders,
            "paper_count": paper_count,
            "live_count": live_count,
            "has_dhan": current_user.dhan_credential is not None and current_user.dhan_credential.is_active,
            "page": page,
            "per_page": per_page,
            "per_page_choices": ORDERS_PER_PAGE_CHOICES,
            "total_orders": total_orders,
            "total_pages": total_pages,
        },
    )


@router.get("/live-pnl")
def live_pnl(db: DbSession, current_user: CurrentUser):
    """JSON endpoint the dashboard polls for live mark-to-market P&L on
    open positions. Never raises to the client — no Dhan connection or a
    failed quote just means an empty/partial result, not an error page."""
    user_strategies = db.scalars(
        select(UserStrategy).where(UserStrategy.user_id == current_user.id)
    ).all()

    try:
        user_dhan = get_user_dhan_client(db, current_user)
    except DhanNotConnectedError:
        return {"positions": []}

    positions = compute_live_pnl(user_dhan.client, user_strategies)
    return {"positions": positions}


@router.post("/pause-all")
def pause_all(request: Request, db: DbSession, current_user: CurrentUser):
    user_strategies = db.scalars(
        select(UserStrategy).where(UserStrategy.user_id == current_user.id, UserStrategy.is_active == True)  # noqa: E712
    ).all()
    for us in user_strategies:
        us.is_active = False
    db.commit()
    flash(request, f"Paused {len(user_strategies)} active strategy(ies).", "success")
    return RedirectResponse(url("/dashboard"), status_code=303)


@router.post("/kill-switch")
def kill_switch(request: Request, db: DbSession, current_user: CurrentUser):
    try:
        user_dhan = get_user_dhan_client(db, current_user)
    except DhanNotConnectedError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url("/dashboard"), status_code=303)

    try:
        # Confirmed against dhan-oss/DhanHQ-py src/dhanhq/_trader_control.py:
        # kill_switch(action) requires an explicit 'ACTIVATE' or 'DEACTIVATE'.
        response = user_dhan.client.kill_switch("ACTIVATE")
        if response.get("status") == "success":
            flash(request, "Dhan kill switch activated — all pending orders on your account will be blocked.", "success")
        else:
            flash(request, f"Kill switch call failed: {response.get('remarks')}", "error")
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Kill switch call failed: {exc}", "error")

    return RedirectResponse(url("/dashboard"), status_code=303)
