"""Live mark-to-market P&L for open positions — read-only, display-only.

Batches every leg across every open position into one `quote_data` call
per exchange segment (via `app.dhan.helpers.fetch_quotes`, which also
handles the double-wrap quirk in Dhan's quote response), since Dhan's
quote API is rate-limited to 1 request/sec (SKILL.md) and this gets
polled from the dashboard.
"""

from __future__ import annotations

from typing import Any

from app.dhan.helpers import fetch_combined_margin, fetch_quotes
from app.engine.runner import find_open_run
from app.models import UserStrategy
from app.strategies.base import leg_pnl


def compute_live_pnl(dhan_client: Any, user_strategies: list[UserStrategy]) -> list[dict]:
    """Total live P&L per open position = realized P&L booked so far
    (from rolls/per-leg exits that already happened this run — `run.
    realized_pnl`) plus unrealized mark-to-market on whatever's still
    open. This mirrors app.strategies.three_pair_rolling.evaluate_exit's
    own daily-SL/target math exactly, on purpose: the dashboard number and
    the number the engine actually trades on must never disagree.

    Before this, a strategy's very first entry_premium (net credit at
    entry) was compared against the current market value of *every* leg
    the run had ever held — including legs a roll had already closed and
    replaced. For a strategy with no rolls that was harmless (legs never
    changes); for a rolling strategy it silently mixed dead history into
    the live number, on top of never accounting for what a roll already
    realized. Only still-open legs (per `leg_state`) are priced here now."""
    open_positions: list[tuple[UserStrategy, list[dict], float, float]] = []
    security_ids_by_segment: dict[str, set[str]] = {}

    for us in user_strategies:
        run = find_open_run(us)
        if run is None:
            continue
        legs_planned = run.legs_planned or {}
        all_legs = legs_planned.get("legs") or []
        entry_premium = legs_planned.get("entry_premium")
        if not all_legs or entry_premium is None:
            continue

        leg_state = legs_planned.get("leg_state") or {}
        open_legs = [leg for leg in all_legs if (leg_state.get(str(leg["security_id"])) or {}).get("status") != "closed"]
        if not open_legs:
            continue  # every leg already closed via roll/per-leg exit; whole-position close will finish it off

        realized_so_far = float(run.realized_pnl or 0)
        open_positions.append((us, open_legs, float(entry_premium), realized_so_far))
        for leg in open_legs:
            security_ids_by_segment.setdefault(leg["exchange_segment"], set()).add(str(leg["security_id"]))

    if not open_positions:
        return []

    # One quote_data call per exchange segment covers every leg of every
    # open position — far cheaper than one call per leg against a 1 req/sec
    # limit that's shared across the whole poll.
    securities = {segment: [int(sid) for sid in sids] for segment, sids in security_ids_by_segment.items()}
    quotes = fetch_quotes(dhan_client, securities)

    results: list[dict] = []
    for us, open_legs, entry_premium, realized_so_far in open_positions:
        unrealized = 0.0
        all_priced = True
        leg_prices: list[dict] = []
        for leg in open_legs:
            sid = str(leg["security_id"])
            quote = quotes.get((leg["exchange_segment"], sid))
            price = float(quote.get("last_price", 0)) if quote is not None else None
            if price is None:
                all_priced = False
            else:
                unrealized += leg_pnl(leg, price)
            leg_prices.append({"security_id": sid, "current_price": price})

        quantity = open_legs[0]["quantity"]
        pnl_total = (realized_so_far + unrealized) if all_priced else None
        # % is relative to the original premium collected at entry — an
        # approximation once a roll has changed what's actually open, but
        # still the only stable per-unit reference point the run has.
        entry_total = entry_premium * quantity
        pnl_pct = (pnl_total / abs(entry_total) * 100) if (pnl_total is not None and entry_total) else None

        results.append(
            {
                "user_strategy_id": str(us.id),
                "strategy_name": us.strategy.name,
                "mode": us.mode.value,
                "entry_premium": entry_premium,
                "quantity": quantity,
                "pnl_total": pnl_total,
                "pnl_pct": pnl_pct,
                "priced": all_priced,
                "legs": leg_prices,
            }
        )

    return results


def compute_combined_margin(dhan_client: Any, user_strategies: list[UserStrategy]) -> list[dict]:
    """Combined margin blocked (with hedge benefit) per open position, via
    `app.dhan.helpers.fetch_combined_margin`.

    Unlike `compute_live_pnl` above, this can't batch every position into
    one shared call — margin_calculator_multi's hedge-benefit netting is
    only meaningful *within* one position's own legs; mixing two unrelated
    strategies' legs into one scrip_list would compute a fabricated
    combined number that doesn't reflect either one's real standalone
    requirement. So this is one throttled call per open position — fine
    on-demand (a Dashboard page load), never called from the scheduler's
    own fast poll loop."""
    results: list[dict] = []
    for us in user_strategies:
        run = find_open_run(us)
        if run is None:
            continue
        legs_planned = run.legs_planned or {}
        all_legs = legs_planned.get("legs") or []
        if not all_legs:
            continue

        leg_state = legs_planned.get("leg_state") or {}
        open_legs = [leg for leg in all_legs if (leg_state.get(str(leg["security_id"])) or {}).get("status") != "closed"]
        if not open_legs:
            continue  # every leg already closed via roll/per-leg exit; whole-position close will finish it off

        margin_total = fetch_combined_margin(dhan_client, open_legs)
        results.append({"user_strategy_id": str(us.id), "margin_total": margin_total})

    return results
