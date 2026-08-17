"""Demo strategy: sell an OTM strangle (CE + PE) N points away from spot on
the nearest expiry, exit on a stop-loss or target measured against the
combined entry premium.

This exists to validate the paper-trading pipeline end to end (login →
connect Dhan → enable strategy → scheduler evaluates → paper order logged).
Balu's real option-selling strategies plug into the same `Strategy`
interface — this file is the template to copy.
"""

from __future__ import annotations

from typing import Any

from app.dhan.helpers import fetch_chain_df, fetch_expiry_list, fetch_quotes, get_lot_size
from app.strategies.base import OrderLeg, Strategy, StrategyContext


class ExampleShortStrangle(Strategy):
    name = "Example: OTM Short Strangle"
    description = (
        "Sells a call and a put N points away from spot on the nearest "
        "expiry. Exits the whole position on a stop-loss or target measured "
        "against the combined entry premium. Demo/template strategy."
    )
    default_params = {
        "underlying_security_id": 13,  # NIFTY 50
        "underlying_exchange_segment": "IDX_I",
        "strike_offset_points": 200,
        "lots": 1,
        "stop_loss_pct": 30,  # exit if combined premium rises 30% (loss for a seller)
        "target_pct": 50,     # exit if combined premium falls 50% (profit for a seller)
        "product_type": "INTRADAY",
    }

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}

        expiry_data = fetch_expiry_list(
            ctx.dhan_client,
            p["underlying_security_id"],
            p["underlying_exchange_segment"],
        )
        if not expiry_data:
            return None
        nearest_expiry = expiry_data[0]

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=p["underlying_security_id"],
            expiry=nearest_expiry,
            under_exchange_segment=p["underlying_exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        ce_strike = min(strikes, key=lambda x: abs(x - (spot + p["strike_offset_points"])))
        pe_strike = min(strikes, key=lambda x: abs(x - (spot - p["strike_offset_points"])))

        ce_matches = chain_df[chain_df["strike"] == ce_strike]
        pe_matches = chain_df[chain_df["strike"] == pe_strike]
        if ce_matches.empty or pe_matches.empty:
            return None
        ce_row = ce_matches.iloc[0]
        pe_row = pe_matches.iloc[0]

        if not ce_row.get("ce_security_id") or not pe_row.get("pe_security_id"):
            return None

        # Resolve lot size from the exact contract, not a fuzzy underlying-name
        # match — see single_leg_seller_hedge.py for why the name-based lookup
        # never actually matches and silently falls back to a stale default.
        lot_size = get_lot_size(security_id=ce_row["ce_security_id"]) or 75
        quantity = lot_size * int(p["lots"])

        return [
            OrderLeg(
                label=f"SELL {int(ce_strike)} CE ({nearest_expiry})",
                security_id=str(ce_row["ce_security_id"]),
                trading_symbol=f"NIFTY {int(ce_strike)} CE {nearest_expiry}",
                exchange_segment="NSE_FNO",
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                product_type=p["product_type"],
                price=float(ce_row["ce_ltp"] or 0),
            ),
            OrderLeg(
                label=f"SELL {int(pe_strike)} PE ({nearest_expiry})",
                security_id=str(pe_row["pe_security_id"]),
                trading_symbol=f"NIFTY {int(pe_strike)} PE {nearest_expiry}",
                exchange_segment="NSE_FNO",
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                product_type=p["product_type"],
                price=float(pe_row["pe_ltp"] or 0),
            ),
        ]

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        entry_premium = open_run_notes.get("entry_premium")
        if not legs or not entry_premium:
            return False

        securities_by_segment: dict[str, list[int]] = {}
        for leg in legs:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

        current_premium = 0.0
        for leg in legs:
            quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
            if quote is None:
                # Can't get a fresh quote for a leg — don't guess; skip this evaluation pass.
                return False
            current_premium += float(quote.get("last_price", 0))

        change_pct = ((current_premium - entry_premium) / entry_premium) * 100
        if change_pct >= p["stop_loss_pct"]:
            return True  # premium rose too much against a net-seller — stop loss
        if change_pct <= -p["target_pct"]:
            return True  # premium decayed enough — target hit
        return False
