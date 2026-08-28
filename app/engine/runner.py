"""Evaluates one user's active strategy and acts on it: paper-fills by
default, or places a real order when explicitly allowed.

Safety rules carried over from the dhanhq-skills SKILL.md, enforced here
rather than trusted to individual strategies:
  - every new UserStrategy starts in paper mode (enforced at the router)
  - live orders require BOTH `user_strategy.mode == LIVE` AND the
    `ALLOW_LIVE_TRADING` master switch to be true
  - LIMIT by default; MARKET only when a strategy instance's own
    `order_type` param is explicitly set to it (Balu's explicit choice,
    see `app.strategies.base.resolve_order_type` — no price protection)
  - lot size is taken from the security master via the strategy itself,
    never hardcoded here
  - every order preview is logged before being placed
  - any exception aborts this run without side effects; it never crashes
    the scheduler loop

An open position's exit rules are evaluated against the params *frozen at
entry time* (`legs_planned.params_snapshot`), not whatever the user's
config currently says — editing a strategy's SL/target while a position is
open must not retroactively change that position's rules.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.dhan.client import DhanNotConnectedError, get_user_dhan_client
from app.dhan.helpers import fetch_quotes, preview_order
from app.email import send_email
from app.models import Order, OrderStatus, StrategyMode, StrategyRun, User, UserStrategy
from app.strategies.base import OrderLeg, StrategyContext, leg_pnl
from app.strategies.registry import get_strategy_class

logger = logging.getLogger("app.engine")

IST = ZoneInfo("Asia/Kolkata")

# Guards the dict below, and only the dict — this is process-local (like
# the Dhan throttle registries in app.dhan.helpers) and resets on restart,
# which is fine: the point is not flooding an inbox with one email per
# 30-second poll tick while an instance keeps failing the same way, not a
# durable delivery record.
_error_alert_lock = threading.Lock()
_last_error_alert_at: dict[uuid.UUID, datetime] = {}
_ERROR_ALERT_COOLDOWN = timedelta(minutes=30)


def _send_strategy_error_alert(user: User, user_strategy_id: uuid.UUID, strategy_name: str, error_text: str) -> None:
    """Email the strategy's owner that a run_user_strategy() evaluation
    pass raised — the same exception already logged with a full traceback
    just above every call site of this function. At most one email per
    user_strategy per _ERROR_ALERT_COOLDOWN, so a persistently-failing
    instance (e.g. a stuck bad expiry) sends one alert per half hour, not
    one per poll tick. Never lets a mail-server hiccup, or this function
    itself, propagate — matches send_email()'s own "never raises" contract
    and this module's rule that no exception here may break the scheduler.
    """
    now = datetime.now(timezone.utc)
    with _error_alert_lock:
        last_sent = _last_error_alert_at.get(user_strategy_id)
        if last_sent is not None and now - last_sent < _ERROR_ALERT_COOLDOWN:
            return
        _last_error_alert_at[user_strategy_id] = now

    try:
        send_email(
            user.email,
            f"[FinOps Algo] Strategy error: {strategy_name}",
            html_body=(
                f"<p>Your strategy <b>{strategy_name}</b> hit an error while running and needs a look:</p>"
                f"<pre style='white-space:pre-wrap'>{error_text}</pre>"
                f"<p>It will keep retrying on its own poll schedule. You won't get another email for this "
                f"instance for {int(_ERROR_ALERT_COOLDOWN.total_seconds() // 60)} minutes even if it keeps failing.</p>"
            ),
            text_body=(
                f"Your strategy '{strategy_name}' hit an error while running and needs a look:\n\n"
                f"{error_text}\n\n"
                f"It will keep retrying on its own poll schedule. You won't get another email for this "
                f"instance for {int(_ERROR_ALERT_COOLDOWN.total_seconds() // 60)} minutes even if it keeps failing."
            ),
        )
    except Exception:  # noqa: BLE001 — an alerting failure must never break strategy evaluation
        logger.exception("Failed to send strategy-error email alert for user_strategy_id=%s", user_strategy_id)


def find_open_run(user_strategy: UserStrategy) -> StrategyRun | None:
    for run in sorted(user_strategy.runs, key=lambda r: r.started_at, reverse=True):
        if run.status == "open":
            return run
    return None


def _today_run_count(user_strategy: UserStrategy) -> int:
    """Counts today's runs toward a strategy's one-entry-per-day cap — every
    run counts, however it ended (automatic exit or manual "Close Now").
    Once you've closed a position today, the scheduler won't open another
    one on its own; `enter_user_strategy_now` (the "Enter Now" button) is
    the only way to trade again the same day, and it bypasses this count
    deliberately, on a single explicit click."""
    today_ist = datetime.now(IST).date()
    return sum(1 for run in user_strategy.runs if run.started_at.astimezone(IST).date() == today_ist)


def _opposite(transaction_type: str) -> str:
    return "BUY" if transaction_type == "SELL" else "SELL"


def _place_or_paper_leg(
    db: Session,
    dhan_client: Any,
    user_id: Any,
    strategy_run_id: Any,
    leg: OrderLeg,
    *,
    is_live: bool,
) -> Order:
    preview = preview_order(
        leg.security_id,
        leg.exchange_segment,
        leg.transaction_type,
        leg.quantity,
        leg.order_type,
        leg.product_type,
        price=leg.price,
        trading_symbol=leg.trading_symbol,
    )
    logger.info("Order preview (%s, %s): %s", "LIVE" if is_live else "PAPER", leg.role, preview)

    dhan_order_id = None
    status = OrderStatus.PAPER_FILLED

    if is_live:
        # A MARKET order carries no price at all -- leg.price (the last-seen
        # LTP) stays on the OrderLeg/Order record itself for paper-fill and
        # P&L math regardless of order_type; only the actual live API call
        # zeroes it out, matching Dhan's own MARKET semantics.
        live_price = 0.0 if leg.order_type == "MARKET" else leg.price
        response = dhan_client.place_order(
            security_id=leg.security_id,
            exchange_segment=leg.exchange_segment,
            transaction_type=leg.transaction_type,
            quantity=leg.quantity,
            order_type=leg.order_type,
            product_type=leg.product_type,
            price=live_price,
        )
        if response.get("status") == "success":
            dhan_order_id = response.get("data", {}).get("orderId")
            status = OrderStatus.PLACED
        else:
            status = OrderStatus.REJECTED
            logger.warning("Live order rejected: %s", response.get("remarks"))

    order = Order(
        user_id=user_id,
        strategy_run_id=strategy_run_id,
        dhan_order_id=dhan_order_id,
        security_id=leg.security_id,
        trading_symbol=leg.trading_symbol,
        transaction_type=leg.transaction_type,
        quantity=leg.quantity,
        order_type=leg.order_type,
        product_type=leg.product_type,
        price=leg.price,
        role=leg.role,
        status=status,
        is_paper=not is_live,
    )
    db.add(order)
    return order


def _open_leg_state(sid: str, leg_state: dict[str, Any]) -> dict[str, Any]:
    return {"status": "open", **(leg_state.get(sid) or {})}


def _close_open_run(
    db: Session,
    dhan_client: Any,
    user_id: Any,
    open_run: StrategyRun,
    *,
    is_live: bool,
    reason: str,
    is_manual: bool = False,
) -> bool:
    """Reverse every leg on `open_run` that isn't already closed (primary
    and hedge alike) and mark the run closed. Shared by the scheduled
    whole-position exit path and the manual "Close Now" path so they
    behave identically (aside from `is_manual`, which only affects whether
    this run counts toward the strategy's one-entry-per-day cap — see
    `_today_run_count`). Legs a strategy already squared off individually
    (a per-leg exit via `evaluate_leg_exits`, or a completed roll via
    `evaluate_rolls` — both tracked in `leg_state`) are skipped; most
    strategies never populate `leg_state`, so every leg defaults to open
    and this behaves exactly as a plain "reverse everything" for them.

    Returns True if the run was actually closed, False if it was left open
    because a fresh quote couldn't be fetched for every leg (see below) —
    callers that need to tell the user something happened (the manual
    "Close Now" button) must check this rather than assume success."""
    notes = open_run.legs_planned or {}
    leg_state = notes.get("leg_state") or {}
    legs_data = [
        leg for leg in notes.get("legs", []) if _open_leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
    ]

    # Price exits off fresh quotes, not the stale entry price — reusing the
    # entry price would make paper P&L meaningless and, in live mode, would
    # place a LIMIT order at a price with no relation to the current
    # market. One batched call for every leg. If any leg's fresh quote
    # can't be fetched, never fall back to its stored entry price — that
    # writes a fabricated "flat" fill indistinguishable from a real one
    # (see app.engine.runner._apply_rolls for the bug this mirrors).
    # Leave the whole run open and let it retry next poll/click instead.
    securities_by_segment: dict[str, list[int]] = {}
    for leg_data in legs_data:
        securities_by_segment.setdefault(leg_data["exchange_segment"], []).append(int(leg_data["security_id"]))
    quotes = fetch_quotes(dhan_client, securities_by_segment)

    missing = [
        leg for leg in legs_data
        if (leg["exchange_segment"], str(leg["security_id"])) not in quotes
    ]
    if missing:
        logger.warning(
            "Skipping close of run %s — could not fetch a fresh exit quote for %s; will retry next poll.",
            open_run.id, [leg["security_id"] for leg in missing],
        )
        return False

    pnl_delta = 0.0
    for leg_data in legs_data:
        quote = quotes[(leg_data["exchange_segment"], str(leg_data["security_id"]))]
        exit_price = float(quote.get("last_price", leg_data["price"]))

        exit_leg = OrderLeg(
            label=f"EXIT {leg_data['label']}",
            security_id=leg_data["security_id"],
            trading_symbol=leg_data["trading_symbol"],
            exchange_segment=leg_data["exchange_segment"],
            transaction_type=_opposite(leg_data["transaction_type"]),
            quantity=leg_data["quantity"],
            order_type=leg_data["order_type"],
            product_type=leg_data["product_type"],
            price=exit_price,
            role=leg_data.get("role", "primary"),
        )
        _place_or_paper_leg(db, dhan_client, user_id, open_run.id, exit_leg, is_live=is_live)
        pnl_delta += leg_pnl(leg_data, exit_price)

    open_run.status = "closed"
    open_run.evaluation_notes = reason
    open_run.manually_closed = is_manual
    open_run.realized_pnl = float(open_run.realized_pnl or 0) + pnl_delta
    open_run.closed_at = datetime.now(timezone.utc)
    db.commit()
    return True


def _execute_entry(
    db: Session,
    dhan_client: Any,
    user_id: Any,
    user_strategy_id: Any,
    legs: list[OrderLeg],
    entry_params: dict[str, Any],
    *,
    is_live: bool,
) -> StrategyRun:
    """Create the StrategyRun and place every leg's order. Shared by the
    scheduler's automatic entry path and the manual "Enter Now" button so
    both produce an identical run/order record."""
    entry_premium = sum(leg.price for leg in legs if leg.transaction_type == "SELL") - sum(
        leg.price for leg in legs if leg.transaction_type == "BUY"
    )

    run = StrategyRun(
        user_strategy_id=user_strategy_id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={
            "legs": [asdict(leg) for leg in legs],
            "entry_premium": entry_premium,
            "params_snapshot": entry_params,
        },
        evaluation_notes=f"Entered {len(legs)} leg(s) in {'LIVE' if is_live else 'PAPER'} mode.",
    )
    db.add(run)
    db.flush()  # assign run.id before orders reference it

    for leg in legs:
        _place_or_paper_leg(db, dhan_client, user_id, run.id, leg, is_live=is_live)

    db.commit()
    return run


def _apply_rolls(
    db: Session,
    dhan_client: Any,
    user_id: Any,
    open_run: StrategyRun,
    decision: dict[str, Any],
    *,
    is_live: bool,
) -> None:
    """Close each named group of legs at a fresh quote and immediately
    open its replacement group, without touching any other leg or ending
    the run. See `Strategy.evaluate_rolls` for the decision shape. New
    legs are appended to `legs_planned["legs"]` (never replacing history)
    so a strategy can always see every strike a given `pair_id` has held
    today — e.g. app.strategies.three_pair_rolling's unique-spot-per-day
    rule depends on this full history, not just what's currently open."""
    notes = dict(open_run.legs_planned or {})  # copy so reassignment below is detected as a change
    legs_data = list(notes.get("legs", []))
    leg_state: dict[str, Any] = {sid: dict(state) for sid, state in (notes.get("leg_state") or {}).items()}

    pnl_delta = 0.0
    any_rolled = False

    for roll in decision.get("rolls") or []:
        close_ids = {str(sid) for sid in (roll.get("close_security_ids") or [])}
        to_close = [
            leg for leg in legs_data
            if str(leg["security_id"]) in close_ids and _open_leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
        ]
        new_legs = roll.get("new_legs") or []
        # A roll normally requires something real currently open to reverse
        # (see test_apply_rolls_ignores_a_roll_with_nothing_currently_open_
        # to_close) — that guard exists to stop a stale decision from
        # half-executing (opening new legs without actually reversing the
        # old ones). A strategy whose legs can also close independently
        # (e.g. a per-leg stop-loss/target) can legitimately reach a roll
        # boundary with nothing left open at the old strike — both of that
        # strike's legs already exited on their own before spot ever got
        # there. That's opted into explicitly via allow_empty_close rather
        # than loosening the guard for every strategy; no existing
        # evaluate_rolls implementation sets it, so this branch is
        # unreachable for them and their behavior is unchanged.
        if not new_legs or (not to_close and not roll.get("allow_empty_close")):
            continue  # nothing valid to do for this roll — skip it, don't half-execute

        if to_close:
            securities_by_segment: dict[str, list[int]] = {}
            for leg in to_close:
                securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
            quotes = fetch_quotes(dhan_client, securities_by_segment)

            # Never close a leg on a fabricated price. Falling back to the
            # stored entry price here (as this used to do) produces a fake
            # "flat" fill indistinguishable from a real one — silently
            # wrong P&L, not an error anyone would notice. If any leg in
            # this roll can't get a fresh quote this pass, skip the whole
            # roll (don't close some legs and not others, and don't open
            # the replacement pair either) and retry next poll — the same
            # "don't guess" contract evaluate_rolls already uses when it
            # can't fetch chain/spot data.
            missing = [
                leg for leg in to_close
                if (leg["exchange_segment"], str(leg["security_id"])) not in quotes
            ]
            if missing:
                logger.warning(
                    "Skipping roll — could not fetch a fresh exit quote for %s; will retry next poll.",
                    [leg["security_id"] for leg in missing],
                )
                continue

            for leg_data in to_close:
                quote = quotes[(leg_data["exchange_segment"], str(leg_data["security_id"]))]
                exit_price = float(quote.get("last_price", leg_data["price"]))

                exit_leg = OrderLeg(
                    label=f"ROLL-CLOSE {leg_data['label']}",
                    security_id=leg_data["security_id"],
                    trading_symbol=leg_data["trading_symbol"],
                    exchange_segment=leg_data["exchange_segment"],
                    transaction_type=_opposite(leg_data["transaction_type"]),
                    quantity=leg_data["quantity"],
                    order_type=leg_data["order_type"],
                    product_type=leg_data["product_type"],
                    price=exit_price,
                    role=leg_data.get("role", "primary"),
                )
                _place_or_paper_leg(db, dhan_client, user_id, open_run.id, exit_leg, is_live=is_live)
                pnl_delta += leg_pnl(leg_data, exit_price)
                sid = str(leg_data["security_id"])
                leg_state[sid] = {**_open_leg_state(sid, leg_state), "status": "closed"}

        for new_leg in new_legs:
            _place_or_paper_leg(db, dhan_client, user_id, open_run.id, new_leg, is_live=is_live)
            legs_data.append(asdict(new_leg))
            leg_state[str(new_leg.security_id)] = {"status": "open"}

        # Opt-in, mirrors _apply_leg_exits's leg_state_patch: lets a roll
        # tag arbitrary sids (open or already closed) — e.g. flagging every
        # leg that was ever part of a rolled-away strike as no longer in
        # the active window, even the ones that closed independently
        # earlier and were therefore never in to_close.
        for sid, patch in (roll.get("leg_state_patch") or {}).items():
            sid = str(sid)
            leg_state[sid] = {**_open_leg_state(sid, leg_state), **patch}

        any_rolled = True

    if not any_rolled:
        return

    notes["legs"] = legs_data
    notes["leg_state"] = leg_state
    open_run.realized_pnl = float(open_run.realized_pnl or 0) + pnl_delta
    notes["realized_pnl_so_far"] = float(open_run.realized_pnl)
    open_run.legs_planned = notes
    open_run.evaluation_notes = f"Rolled {len(decision.get('rolls') or [])} pair(s)."
    db.commit()


def _apply_leg_exits(
    db: Session,
    dhan_client: Any,
    user_id: Any,
    open_run: StrategyRun,
    decision: dict[str, Any],
    *,
    is_live: bool,
) -> None:
    """Reverse only the legs a strategy's `evaluate_leg_exits` named, apply
    its `leg_state_patch`, and close the whole run once no primary-role leg
    is left open. See `Strategy.evaluate_leg_exits` for the decision shape."""
    notes = dict(open_run.legs_planned or {})  # copy so reassignment below is detected as a change
    legs_data = notes.get("legs", [])
    leg_state: dict[str, Any] = {sid: dict(state) for sid, state in (notes.get("leg_state") or {}).items()}

    close_ids = {str(sid) for sid in (decision.get("close_security_ids") or [])}
    to_close = [
        leg for leg in legs_data
        if str(leg["security_id"]) in close_ids and _open_leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
    ]

    pnl_delta = 0.0
    if to_close:
        securities_by_segment: dict[str, list[int]] = {}
        for leg in to_close:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(dhan_client, securities_by_segment)

        # Same "don't fabricate a price" rule as _apply_rolls and
        # _close_open_run: if any leg due to close can't get a fresh
        # quote, abort this whole decision — no legs closed, no
        # leg_state_patch applied — and retry next poll rather than
        # write an exit order at a price that isn't real.
        missing = [
            leg for leg in to_close
            if (leg["exchange_segment"], str(leg["security_id"])) not in quotes
        ]
        if missing:
            logger.warning(
                "Skipping leg-exit on run %s — could not fetch a fresh exit quote for %s; will retry next poll.",
                open_run.id, [leg["security_id"] for leg in missing],
            )
            return

        for leg_data in to_close:
            quote = quotes[(leg_data["exchange_segment"], str(leg_data["security_id"]))]
            exit_price = float(quote.get("last_price", leg_data["price"]))

            exit_leg = OrderLeg(
                label=f"EXIT {leg_data['label']}",
                security_id=leg_data["security_id"],
                trading_symbol=leg_data["trading_symbol"],
                exchange_segment=leg_data["exchange_segment"],
                transaction_type=_opposite(leg_data["transaction_type"]),
                quantity=leg_data["quantity"],
                order_type=leg_data["order_type"],
                product_type=leg_data["product_type"],
                price=exit_price,
                role=leg_data.get("role", "primary"),
            )
            _place_or_paper_leg(db, dhan_client, user_id, open_run.id, exit_leg, is_live=is_live)
            pnl_delta += leg_pnl(leg_data, exit_price)
            sid = str(leg_data["security_id"])
            leg_state[sid] = {**_open_leg_state(sid, leg_state), "status": "closed"}

    for sid, patch in (decision.get("leg_state_patch") or {}).items():
        sid = str(sid)
        leg_state[sid] = {**_open_leg_state(sid, leg_state), **patch}

    notes["leg_state"] = leg_state
    open_run.legs_planned = notes
    open_run.realized_pnl = float(open_run.realized_pnl or 0) + pnl_delta

    primary_ids = {str(leg["security_id"]) for leg in legs_data if leg.get("role") == "primary"}
    still_open_primary = [sid for sid in primary_ids if leg_state.get(sid, {"status": "open"})["status"] == "open"]

    if to_close and not still_open_primary:
        open_run.status = "closed"
        open_run.evaluation_notes = "All primary legs closed via per-leg exit rules."
        open_run.closed_at = datetime.now(timezone.utc)
    elif to_close:
        open_run.evaluation_notes = f"Partial exit: closed {len(to_close)} leg(s) on per-leg exit rules."
    db.commit()


def run_user_strategy(db: Session, user_strategy: UserStrategy) -> None:
    if not user_strategy.is_active:
        return

    user = user_strategy.user
    strategy = user_strategy.strategy

    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
    except ValueError:
        logger.error("Strategy %s has unknown code_ref=%s", strategy.id, strategy.code_ref)
        return

    try:
        user_dhan = get_user_dhan_client(db, user)
    except DhanNotConnectedError:
        logger.info("Skipping %s for %s: no active Dhan connection", strategy.name, user.email)
        return

    settings = get_settings()
    is_live = user_strategy.mode == StrategyMode.LIVE and settings.allow_live_trading
    impl = strategy_cls()

    try:
        open_run = find_open_run(user_strategy)

        if open_run is not None:
            # Exit rules use the params frozen at entry, not the live config.
            snapshot_params = (open_run.legs_planned or {}).get("params_snapshot") or {
                **strategy.default_params,
                **user_strategy.params,
            }
            exit_ctx = StrategyContext(dhan_client=user_dhan.client, params=snapshot_params)

            should_exit = impl.evaluate_exit(exit_ctx, open_run.legs_planned or {})
            if should_exit:
                _close_open_run(
                    db,
                    user_dhan.client,
                    user.id,
                    open_run,
                    is_live=is_live,
                    reason="Exit conditions met; opposite-side orders placed for all legs.",
                )
                return

            # Whole-position exit didn't fire — give strategies that manage
            # legs independently a chance to act on a subset of them: close
            # some (e.g. a per-leg stop-loss) and/or roll a group (close +
            # immediately reopen its replacement), without ending the run.
            leg_decision = impl.evaluate_leg_exits(exit_ctx, open_run.legs_planned or {})
            if leg_decision:
                _apply_leg_exits(db, user_dhan.client, user.id, open_run, leg_decision, is_live=is_live)

            roll_decision = impl.evaluate_rolls(exit_ctx, open_run.legs_planned or {})
            if roll_decision:
                _apply_rolls(db, user_dhan.client, user.id, open_run, roll_decision, is_live=is_live)
            return

        entry_params = {**strategy.default_params, **user_strategy.params}
        entry_ctx = StrategyContext(
            dhan_client=user_dhan.client,
            params=entry_params,
            today_run_count=_today_run_count(user_strategy),
        )

        legs = impl.evaluate_entry(entry_ctx)
        if not legs:
            return

        _execute_entry(db, user_dhan.client, user.id, user_strategy.id, legs, entry_params, is_live=is_live)

    except Exception as exc:
        db.rollback()
        logger.exception("Error evaluating strategy %s for user %s", strategy.name, user.email)
        _send_strategy_error_alert(user, user_strategy.id, strategy.name, f"{type(exc).__name__}: {exc}")


def close_user_strategy_now(db: Session, user_strategy: UserStrategy) -> bool:
    """Immediately close an open position for this strategy, outside the
    normal poll cycle — the "Close Now" button's action. Returns True if a
    position was found and closed, False if there was nothing open.

    Raises RuntimeError if a position was found but couldn't actually be
    closed this attempt (a fresh quote wasn't available for every leg) —
    distinct from "nothing to close", so the caller doesn't tell the user
    their position closed when it's still open. Safe to click again."""
    open_run = find_open_run(user_strategy)
    if open_run is None:
        return False

    user = user_strategy.user
    settings = get_settings()
    is_live = user_strategy.mode == StrategyMode.LIVE and settings.allow_live_trading

    try:
        user_dhan = get_user_dhan_client(db, user)
    except DhanNotConnectedError:
        raise

    closed = _close_open_run(
        db,
        user_dhan.client,
        user.id,
        open_run,
        is_live=is_live,
        reason="Manually closed by user.",
        is_manual=True,
    )
    if not closed:
        raise RuntimeError(
            "Could not fetch a fresh market quote for every leg — nothing was closed. Please try again in a moment."
        )
    return True


def enter_user_strategy_now(db: Session, user_strategy: UserStrategy) -> bool:
    """Manually trigger an entry check right now, outside the normal poll
    cycle — the "Enter Now" button's action. Runs the exact same
    `evaluate_entry` logic the scheduler uses (still requires the
    strategy's real trigger conditions to actually be met — this is not a
    blind market order), but deliberately passes `today_run_count=0`,
    ignoring how many times this instance has already traded today.

    That's a deliberate override: once a position closes (automatically
    *or* manually), the scheduler will not re-enter this instance again on
    its own for the rest of the day (see `_today_run_count`) — this is the
    one explicit, single-click way around that, for exactly the attempt
    the user asked for right now.

    Returns True if a position was entered, False if conditions aren't
    currently met (not an error — just "not yet"). Raises ValueError if
    there's already an open position (close it first), or
    DhanNotConnectedError if there's no active Dhan connection — same
    error contract as `close_user_strategy_now`."""
    if find_open_run(user_strategy) is not None:
        raise ValueError("This instance already has an open position — close it first.")

    user = user_strategy.user
    strategy = user_strategy.strategy
    strategy_cls = get_strategy_class(strategy.code_ref)

    user_dhan = get_user_dhan_client(db, user)  # raises DhanNotConnectedError

    settings = get_settings()
    is_live = user_strategy.mode == StrategyMode.LIVE and settings.allow_live_trading
    impl = strategy_cls()

    entry_params = {**strategy.default_params, **user_strategy.params}
    entry_ctx = StrategyContext(dhan_client=user_dhan.client, params=entry_params, today_run_count=0)

    legs = impl.evaluate_entry(entry_ctx)
    if not legs:
        return False

    _execute_entry(db, user_dhan.client, user.id, user_strategy.id, legs, entry_params, is_live=is_live)
    return True
