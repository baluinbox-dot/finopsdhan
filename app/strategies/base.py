"""Strategy plugin interface.

Every strategy Balu adds — the real option-selling strategies as well as
this demo — implements `Strategy`. Strategies only ever *decide what to
trade*; they never call `place_order` themselves. `app/engine/runner.py`
owns execution (paper vs live), persistence, and the SKILL.md safety rules
(LIMIT default, notional warnings, lot-size validation, confirmation gating).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from dhanhq import dhanhq


@dataclass
class OrderLeg:
    label: str
    security_id: str
    trading_symbol: str
    exchange_segment: str
    transaction_type: str  # BUY / SELL
    quantity: int
    order_type: str = "LIMIT"
    product_type: str = "INTRADAY"
    price: float = 0.0
    role: str = "primary"  # "primary" | "hedge" — bookkeeping only, doesn't affect execution
    # Groups legs into an independently-managed sub-position within one run
    # — e.g. app.strategies.three_pair_rolling's FIN1/FIN2/FIN3 pairs, each
    # rolled independently while the other pairs' legs are untouched. None
    # for every strategy that doesn't need this (the overwhelming majority).
    pair_id: str | None = None


def resolve_order_type(params: dict[str, Any]) -> str:
    """Every strategy's `order_type` param, validated to exactly "LIMIT" or
    "MARKET" (defaulting to "LIMIT" — Balu's own explicit choice, not this
    codebase's original safety default — for anything missing/unrecognized).
    Call once per leg-building method (`p = order_type = resolve_order_type(p)`
    pattern) and pass the result to every `OrderLeg(order_type=...)` in that
    method, so a whole entry/roll is internally consistent. MARKET means no
    price protection at all — the order fills at whatever price is
    available, which can be materially worse than the last quote on a thin
    strike; LIMIT (the original default) fills at-or-better than the price
    set or not at all. See app/engine/runner.py's `_place_or_paper_leg` for
    how a MARKET leg's *live* order is actually placed (price zeroed there,
    not here — `leg.price` itself stays the real LTP snapshot everywhere
    else, e.g. paper fills and P&L math, regardless of order_type)."""
    value = str(params.get("order_type") or "LIMIT").upper()
    return value if value in ("LIMIT", "MARKET") else "LIMIT"


def leg_pnl(leg_data: dict[str, Any], reference_price: float) -> float:
    """Realized (or mark-to-market, if `reference_price` is a live quote
    rather than an actual exit fill) P&L in rupees for one leg, given its
    own entry price/quantity/side. A SELL entry profits when the reference
    price is lower (bought back for less than collected); a BUY entry
    (e.g. a hedge) profits when it's higher (sold for more than paid).
    Shared by the engine (computing realized P&L on close) and any
    strategy that needs to mark its own open legs to market (e.g. for a
    live running-total stop-loss/target across several legs)."""
    entry_price = float(leg_data["price"])
    quantity = leg_data["quantity"]
    if leg_data["transaction_type"] == "SELL":
        return (entry_price - reference_price) * quantity
    return (reference_price - entry_price) * quantity


def leg_strike(leg_data: dict[str, Any]) -> float | None:
    """Strike parsed from the trading_symbol every strategy in this app
    builds itself (`"{underlying} {strike} {CE|PE} {expiry}"`) — safe only
    because every UNDERLYINGS key is a single space-free token. Several
    strategy modules keep their own private copy of this same parse (e.g.
    iron_condor_rolling.py's `_strike_of`); this shared version exists for
    code that isn't tied to one specific strategy, like the dashboard's
    max-profit/max-loss calculator in app/engine/pnl.py."""
    tokens = (leg_data.get("trading_symbol") or "").split()
    if len(tokens) < 2:
        return None
    try:
        return float(tokens[1])
    except ValueError:
        return None


def leg_option_type(leg_data: dict[str, Any]) -> str | None:
    """CE/PE parsed off the same trading_symbol convention as `leg_strike`."""
    tokens = (leg_data.get("trading_symbol") or "").split()
    return tokens[2] if len(tokens) >= 3 and tokens[2] in ("CE", "PE") else None


@dataclass
class StrategyContext:
    dhan_client: dhanhq
    params: dict[str, Any] = field(default_factory=dict)
    # Number of StrategyRuns already recorded today for this user's strategy
    # instance (open or closed). Lets a strategy implement its own daily
    # entry cap (e.g. one-shot-per-day) without engine-wide hardcoding.
    today_run_count: int = 0


class Strategy(ABC):
    name: str = "Unnamed Strategy"
    description: str = ""
    default_params: dict[str, Any] = {}

    @abstractmethod
    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        """Return legs to enter now, or None if entry conditions aren't met."""

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        """Return True if the *entire* currently open position (described by
        `open_run_notes`, taken from the entry run's `legs_planned`) should
        be exited now — every leg still open gets reversed. Default: never
        exit automatically."""
        return False

    def evaluate_rolls(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        """Opt-in hook for strategies that roll a subset of legs within an
        already-open run — closing a group of legs (e.g. one `pair_id`)
        and immediately opening a replacement group, without touching any
        other leg or ending the run. Called by the engine only when
        `evaluate_exit` didn't already decide to close everything.

        Return None to do nothing this pass. Return a dict to act:
            {"rolls": [
                {"close_security_ids": [...], "new_legs": [OrderLeg, ...]},
                ...
            ]}
        Each `close_security_ids` entry must currently be open (per
        `open_run_notes`'s `leg_state`, same convention as
        `evaluate_leg_exits`); the engine reverses them at a fresh quote,
        places the `new_legs`' entry orders, and appends the new legs to
        the run's leg history — so a strategy can look back at every
        strike a given `pair_id` has ever held today, not just its current
        one. Default: not implemented — strategies that don't roll never
        call this."""
        return None

    def evaluate_leg_exits(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        """Opt-in hook for strategies that manage legs *independently*
        within one open position — e.g. a per-leg stop-loss where hitting
        one leg's SL squares off only that leg (and its own hedge) while
        the other leg stays open, possibly with its stop trailed to cost.
        Called by the engine only when `evaluate_exit` didn't already
        decide to close everything.

        `open_run_notes` carries whatever `evaluate_leg_exits` previously
        asked the engine to persist, under `"leg_state"` (keyed by
        security_id, e.g. `{"status": "open"|"closed", ...strategy-defined
        fields}`) — a security_id absent from `leg_state` is treated as
        still open. Return None to do nothing this pass. Return a dict to
        act:
            {
                "close_security_ids": [...],   # legs to reverse right now
                "leg_state_patch": {sid: {...}},  # shallow-merged into
                                                   # leg_state for *any*
                                                   # security_id (open or
                                                   # being closed) — e.g.
                                                   # trailing a surviving
                                                   # leg's stop to cost.
            }
        The engine marks every id in `close_security_ids` as `"status":
        "closed"` automatically; when no primary-role leg is left open
        afterwards, the whole run is closed. Default: not implemented —
        strategies that don't need per-leg management never call this."""
        return None
