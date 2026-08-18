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
        """Return True if the currently open position (described by
        `open_run_notes`, taken from the entry run's `legs_planned`) should
        be exited now. Default: never exit automatically."""
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
