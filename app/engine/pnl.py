"""Live mark-to-market P&L for open positions — read-only, display-only.

Batches every leg across every open position into one `quote_data` call
per exchange segment (via `app.dhan.helpers.fetch_quotes`, which also
handles the double-wrap quirk in Dhan's quote response), since Dhan's
quote API is rate-limited to 1 request/sec (SKILL.md) and this gets
polled from the dashboard.
"""

from __future__ import annotations

from typing import Any

from app.dhan.helpers import fetch_quotes
from app.engine.runner import find_open_run
from app.models import UserStrategy


def compute_live_pnl(dhan_client: Any, user_strategies: list[UserStrategy]) -> list[dict]:
    open_positions: list[tuple[UserStrategy, dict, list[dict], float]] = []
    security_ids_by_segment: dict[str, set[str]] = {}

    for us in user_strategies:
        run = find_open_run(us)
        if run is None:
            continue
        legs_planned = run.legs_planned or {}
        legs = legs_planned.get("legs") or []
        entry_premium = legs_planned.get("entry_premium")
        if not legs or entry_premium is None:
            continue
        open_positions.append((us, run, legs, float(entry_premium)))
        for leg in legs:
            security_ids_by_segment.setdefault(leg["exchange_segment"], set()).add(str(leg["security_id"]))

    if not open_positions:
        return []

    # One quote_data call per exchange segment covers every leg of every
    # open position — far cheaper than one call per leg against a 1 req/sec
    # limit that's shared across the whole poll.
    securities = {segment: [int(sid) for sid in sids] for segment, sids in security_ids_by_segment.items()}
    quotes = fetch_quotes(dhan_client, securities)

    results: list[dict] = []
    for us, run, legs, entry_premium in open_positions:
        current_value = 0.0
        all_priced = True
        for leg in legs:
            quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
            if quote is None:
                all_priced = False
                continue
            price = float(quote.get("last_price", 0))
            sign = 1 if leg["transaction_type"] == "SELL" else -1
            current_value += sign * price

        quantity = legs[0]["quantity"] if legs else 0
        pnl_per_unit = (entry_premium - current_value) if all_priced else None
        pnl_total = (pnl_per_unit * quantity) if pnl_per_unit is not None else None
        pnl_pct = (pnl_per_unit / entry_premium * 100) if (pnl_per_unit is not None and entry_premium) else None

        results.append(
            {
                "user_strategy_id": str(us.id),
                "strategy_name": us.strategy.name,
                "mode": us.mode.value,
                "entry_premium": entry_premium,
                "current_value": current_value if all_priced else None,
                "quantity": quantity,
                "pnl_total": pnl_total,
                "pnl_pct": pnl_pct,
                "priced": all_priced,
            }
        )

    return results
