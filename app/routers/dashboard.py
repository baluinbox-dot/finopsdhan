"""Dashboard: active strategies, recent orders/paper-fills, basic P&L,
and safety controls (pause-all, kill switch)."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

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


def _pair_orders(orders: list[Order]) -> tuple[list[Order], list[tuple[Order, Order]]]:
    """Split a user's orders into still-open legs and entry/exit pairs.

    Every leg is opened by exactly one order and, once it closes, reversed
    by exactly one more (see app/engine/runner.py — every close/roll/leg-
    exit path always places the opposite-side order at the same
    security_id). Grouping by (strategy_run_id, security_id) and pairing
    consecutively by fill time recovers that structure directly from the
    order history itself — no separate bookkeeping needed, and it holds
    equally for a plain single-shot strategy, a rolled-away leg inside a
    still-open run, and a fully-closed run."""
    groups: dict[tuple[str, str], list[Order]] = defaultdict(list)
    for o in orders:
        run_key = str(o.strategy_run_id) if o.strategy_run_id else f"_norun_{o.id}"
        groups[(run_key, o.security_id)].append(o)

    running: list[Order] = []
    closed: list[tuple[Order, Order]] = []
    for group in groups.values():
        group.sort(key=lambda o: o.placed_at)
        for i in range(0, len(group) - 1, 2):
            closed.append((group[i], group[i + 1]))
        if len(group) % 2 == 1:
            running.append(group[-1])

    running.sort(key=lambda o: o.placed_at, reverse=True)
    closed.sort(key=lambda pair: pair[1].placed_at, reverse=True)
    return running, closed


def _leg_pnl(entry: Order, exit_: Order) -> float:
    if entry.transaction_type == "SELL":
        return (float(entry.price) - float(exit_.price)) * entry.quantity
    return (float(exit_.price) - float(entry.price)) * entry.quantity


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

    # Pairing needs every order (an entry can be arbitrarily far behind its
    # exit), so this loads the user's full order history rather than one
    # page at a time — fine at this app's per-tenant order volume; revisit
    # with a real SQL pairing query if that ever stops being true.
    all_orders = db.scalars(
        select(Order).where(Order.user_id == current_user.id).order_by(Order.placed_at.asc())
    ).all()
    running_orders, closed_pairs = _pair_orders(all_orders)

    total_closed = len(closed_pairs)
    total_pages = max(1, -(-total_closed // per_page))  # ceil division
    page = min(page, total_pages)
    paged_closed = closed_pairs[(page - 1) * per_page : (page - 1) * per_page + per_page]
    closed_rows = [
        {"entry": entry, "exit": exit_, "pnl": _leg_pnl(entry, exit_)} for entry, exit_ in paged_closed
    ]

    # Each running leg needs its owning strategy instance so the dashboard
    # JS can match it to the live-quote poll's per-leg price (keyed
    # "{user_strategy_id}:{security_id}" — see live_pnl.js).
    running_rows = [
        {"order": o, "user_strategy_id": str(o.strategy_run.user_strategy_id) if o.strategy_run else None}
        for o in running_orders
    ]

    # The two stat-card counts ("Paper Orders (recent)" / "Live Orders
    # (recent)") intentionally still summarize a fixed recent window, not
    # the current page — they're headline counts, not tied to whichever
    # page/page-size the Closed Orders table happens to be showing.
    paper_count = sum(1 for o in all_orders[-25:] if o.is_paper)
    live_count = sum(1 for o in all_orders[-25:] if not o.is_paper)

    return render(
        request,
        "dashboard/index.html",
        {
            "current_user": current_user,
            "user_strategies": user_strategies,
            "active_count": active_count,
            "open_run_ids": open_run_ids,
            "running_rows": running_rows,
            "closed_rows": closed_rows,
            "paper_count": paper_count,
            "live_count": live_count,
            "has_dhan": current_user.dhan_credential is not None and current_user.dhan_credential.is_active,
            "page": page,
            "per_page": per_page,
            "per_page_choices": ORDERS_PER_PAGE_CHOICES,
            "total_orders": total_closed,
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
