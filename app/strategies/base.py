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
