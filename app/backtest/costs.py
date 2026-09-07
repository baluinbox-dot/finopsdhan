"""Transaction cost model for the backtest engine.

The live/paper engine (app.engine.runner) applies none of this -- a paper
fill is the raw quote, full stop. That's fine for watching a strategy's
raw decision quality day to day, but it means a backtest that also just
used raw quotes would look better than real trading ever could,
especially for an option-selling strategy where costs are a meaningful
share of the (often small) premium being collected. This module is
deliberately simple and explicit (per the reference cost-model guidance
this backtest effort started from) rather than a precise reproduction of
current NSE/BSE F&O charges, which are numerous and change over time —
treat every rate below as an approximation to sanity-check strategy
economics, not a real brokerage's actual bill.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    brokerage_per_order: float = 20.0  # flat, per executed leg (typical discount-broker flat fee)
    stt_sell_pct: float = 0.1  # STT on the SELL side of an option trade, % of premium value
    other_charges_pct: float = 0.05  # exchange txn charges + GST + SEBI + stamp duty, lumped, % of premium value
    slippage_bps: float = 5.0  # basis points of unfavorable price movement applied to every fill

    def fill_price(self, quoted_price: float, transaction_type: str) -> float:
        """The price actually filled at, after slippage -- worse than the
        quoted price in the direction that hurts: a SELL fills lower, a
        BUY fills higher."""
        adj = quoted_price * (self.slippage_bps / 10_000)
        return quoted_price - adj if transaction_type == "SELL" else quoted_price + adj

    def order_cost(self, fill_price: float, quantity: int, transaction_type: str) -> float:
        """Total rupee cost (brokerage + STT + other charges) for one
        executed order -- always a positive number, to be subtracted from
        P&L regardless of which side it was on."""
        premium_value = fill_price * quantity
        stt = premium_value * (self.stt_sell_pct / 100) if transaction_type == "SELL" else 0.0
        other = premium_value * (self.other_charges_pct / 100)
        return self.brokerage_per_order + stt + other
