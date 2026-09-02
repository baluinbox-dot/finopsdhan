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
from app.strategies.base import currently_open_legs, leg_option_type, leg_pnl, leg_strike


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
        open_legs = currently_open_legs(all_legs, leg_state)
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
        open_legs = currently_open_legs(all_legs, leg_state)
        if not open_legs:
            continue  # every leg already closed via roll/per-leg exit; whole-position close will finish it off

        margin_total = fetch_combined_margin(dhan_client, open_legs)
        results.append({"user_strategy_id": str(us.id), "margin_total": margin_total})

    return results


def _expiry_payoff_at(open_legs: list[dict], spot: float) -> float | None:
    """Total intrinsic-value P&L, in rupees, across `open_legs` if the
    underlying settled at `spot` at expiry — the entry premium already
    collected/paid on each leg, plus/minus what that leg would be worth at
    that spot. None if any leg's strike/option type can't be parsed (don't
    guess)."""
    total = 0.0
    for leg in open_legs:
        strike = leg_strike(leg)
        option_type = leg_option_type(leg)
        if strike is None or option_type is None:
            return None
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        price = float(leg["price"])
        quantity = leg["quantity"]
        if leg["transaction_type"] == "SELL":
            total += (price - intrinsic) * quantity
        else:
            total += (intrinsic - price) * quantity
    return total


def _expiry_payoff_slope(open_legs: list[dict], *, direction: str) -> float:
    """Constant slope (d P&L / d spot) of the piecewise-linear expiry
    payoff in the unbounded region beyond every leg's strike — `"right"`
    for spot above the highest strike, `"left"` for spot below the lowest.
    A non-zero slope there means P&L runs away to +/-infinity in that
    direction (an uncapped side, e.g. a naked sold option); the breakpoint
    evaluations in `strategy_payoff_extremes` only find the true max/min
    when both slopes are accounted for."""
    slope = 0.0
    for leg in open_legs:
        option_type = leg_option_type(leg)
        if option_type is None:
            continue
        quantity = leg["quantity"]
        sign = 1 if leg["transaction_type"] == "BUY" else -1
        # CE intrinsic's slope wrt spot is 1 on the right (spot > strike),
        # 0 on the left. PE intrinsic's slope is -1 on the left, 0 on the
        # right. A leg's own P&L slope is sign * (that intrinsic slope).
        if option_type == "CE" and direction == "right":
            slope += sign * quantity
        elif option_type == "PE" and direction == "left":
            slope += sign * -quantity
    return slope


def strategy_payoff_extremes(open_legs: list[dict], realized_so_far: float = 0.0) -> dict[str, float | None]:
    """Best-case and worst-case total P&L (in rupees) for `open_legs` held
    to expiry, computed purely from strikes/premiums/quantities — no live
    quote needed. `realized_so_far` (e.g. `StrategyRun.realized_pnl` from
    an earlier roll/leg-exit this run) is added as a flat offset to both,
    since that money is already locked in regardless of where spot ends up.

    The combined payoff of any set of CE/PE legs is piecewise-linear in
    spot, with a kink only at each leg's own strike — so its extremes over
    every possible spot price are found by evaluating P&L at each distinct
    strike, plus checking whether the two unbounded tails (spot -> 0 and
    spot -> infinity) run away to +/-infinity (a non-zero slope there,
    from an uncapped leg like a naked sold option). This one calculation
    is intentionally strategy-agnostic — it works the same way for a
    defined-risk Iron Condor/Fly (bounded both sides) as it does for a
    naked Single-Leg Seller (unbounded on its sold side), without any
    strategy-specific formula.

    Returns `{"max_profit": float|None, "max_loss": float|None}` — both
    signed (max_loss is normally <= 0), `None` meaning that side is
    unbounded ("Unlimited"). `{"max_profit": None, "max_loss": None}` if
    `open_legs` is empty or a leg's strike/option type can't be parsed."""
    if not open_legs:
        return {"max_profit": None, "max_loss": None}

    leg_strikes = [leg_strike(leg) for leg in open_legs]
    if any(s is None for s in leg_strikes):
        return {"max_profit": None, "max_loss": None}  # a strike failed to parse — don't guess
    strikes = sorted(set(leg_strikes))

    breakpoint_values = [_expiry_payoff_at(open_legs, k) for k in strikes]
    if any(v is None for v in breakpoint_values):
        return {"max_profit": None, "max_loss": None}  # an option type failed to parse — don't guess

    values = [v + realized_so_far for v in breakpoint_values]  # type: ignore[operator]
    left_slope = _expiry_payoff_slope(open_legs, direction="left")
    right_slope = _expiry_payoff_slope(open_legs, direction="right")

    max_profit_unlimited = left_slope < 0 or right_slope > 0
    max_loss_unlimited = left_slope > 0 or right_slope < 0

    return {
        "max_profit": None if max_profit_unlimited else max(values),
        "max_loss": None if max_loss_unlimited else min(values),
    }


def compute_max_profit_loss(user_strategies: list[UserStrategy]) -> list[dict]:
    """Max-profit/max-loss (held-to-expiry, at current strikes) for every
    currently-open position — no Dhan client needed at all, so this is
    cheap enough to compute synchronously on every Dashboard page load
    rather than needing its own polled endpoint like live-pnl/margin do."""
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
        open_legs = currently_open_legs(all_legs, leg_state)
        if not open_legs:
            continue

        extremes = strategy_payoff_extremes(open_legs, realized_so_far=float(run.realized_pnl or 0))
        results.append({"user_strategy_id": str(us.id), **extremes})

    return results
