"""ATM straddle seller with a premium-rise entry trigger, independent
per-leg stop-loss, and same-side hedges.

Requirements this implements (Balu's spec, paraphrased):
  - At/after a configured entry time, watch the ATM CE + ATM PE combined
    live premium against a user-supplied reference premium. Sell both legs
    only once the combined premium has risen far enough above that
    reference — either by a percentage or by a flat number of points added
    to it, whichever mode is selected.
  - Each sold leg carries its *own* stop-loss (% above its own entry
    price). If one leg's SL hits, only that leg (and its own hedge, if
    any) is squared off — the other sold leg stays open, with its stop
    trailed to its own entry price (breakeven) from then on.
  - A target measured on the *combined* premium (vs. the original two-leg
    entry combined premium) closes everything still open, hedges included.
  - Everything still open is force-closed at a configured close time.
  - One entry per day (`ctx.today_run_count`, same convention as
    single_leg_seller_hedge.py).

The per-leg SL/trailing behaviour needs the engine's opt-in
`evaluate_leg_exits` hook (see app/strategies/base.py) — `evaluate_exit`
alone can only close the *whole* position, not one leg of it.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_quotes, find_strike_by_nearest_premium, get_lot_size
from app.strategies.base import OrderLeg, Strategy, StrategyContext, currently_open_legs, resolve_order_type

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "00:00").split(":")
    return dt_time(int(hour), int(minute))


def _side_of(leg: dict) -> str | None:
    """CE/PE side of a leg, read off the trading_symbol this strategy
    builds itself (`"{underlying} {strike} {CE|PE} {expiry}"`). Safe only
    because every UNDERLYINGS key is a single space-free token — if that
    ever changes this needs a real field on OrderLeg instead."""
    tokens = (leg.get("trading_symbol") or "").split()
    return tokens[2] if len(tokens) >= 3 and tokens[2] in ("CE", "PE") else None


def _leg_state(sid: str, leg_state: dict) -> dict:
    return {"status": "open", "sl_moved_to_cost": False, **(leg_state.get(sid) or {})}


class ATMStraddleTriggerHedge(Strategy):
    name = "ATM Straddle Seller — Premium Trigger with Hedge"
    description = (
        "Waits for the ATM CE+PE combined premium to rise above a "
        "reference price (by % or a flat amount) before selling both "
        "legs, each with its own same-side hedge. Each sold leg carries "
        "an independent stop-loss — if one hits, only that leg (and its "
        "hedge) closes, and the surviving leg's stop trails to its own "
        "entry price. A combined-premium target closes everything that's "
        "still open. Force-closes at a configured close time. One entry "
        "per day."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "lots": 1,
        "entry_time": "09:15",
        "close_time": "15:15",
        # Combined ATM CE+PE premium observed around entry time — a
        # required input, not something the strategy guesses. Set this
        # each trading day before enabling; entry is blocked while it's 0.
        "reference_premium": 0,
        "entry_trigger_mode": "pct",  # "pct" | "flat"
        "entry_trigger_pct": 10,  # combined premium must reach reference * (1 + pct/100)
        "entry_trigger_flat": 0,  # combined premium must reach reference + flat points (mode == "flat")
        "leg_stop_loss_pct": 25,  # independent SL per sold leg, % above its own entry price
        "target_pct": 80,  # exit everything once combined premium falls this % from entry
        "hedge_enabled": True,
        "hedge_premium_target": 5,  # buy the closest-premium CE/PE hedge to this price
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}
        order_type = resolve_order_type(p)

        now_ist = _now_ist()
        entry_time = _parse_hhmm(p["entry_time"])
        close_time = _parse_hhmm(p["close_time"])
        if not (entry_time <= now_ist.time() < close_time):
            return None

        if ctx.today_run_count > 0:
            return None  # one entry per day already used

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None

        expiry = p.get("expiry")
        if not expiry:
            return None

        reference_premium = float(p.get("reference_premium") or 0)
        if reference_premium <= 0:
            return None  # required input not configured yet

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        atm_strike = min(strikes, key=lambda x: abs(x - spot))
        atm_index = strikes.index(atm_strike)
        atm_matches = chain_df[chain_df["strike"] == atm_strike]
        if atm_matches.empty:
            return None
        atm_row = atm_matches.iloc[0]

        ce_price = atm_row.get("ce_ltp")
        pe_price = atm_row.get("pe_ltp")
        if ce_price is None or pe_price is None or not atm_row.get("ce_security_id") or not atm_row.get("pe_security_id"):
            return None

        combined_premium = float(ce_price) + float(pe_price)

        mode = p.get("entry_trigger_mode", "pct")
        if mode == "flat":
            trigger_level = reference_premium + float(p.get("entry_trigger_flat") or 0)
        else:
            trigger_level = reference_premium * (1 + float(p.get("entry_trigger_pct") or 0) / 100)

        if combined_premium < trigger_level:
            return None  # not triggered yet — keep monitoring on the next poll

        lot_size = get_lot_size(security_id=atm_row["ce_security_id"]) or 75
        quantity = lot_size * int(p["lots"])

        legs = [
            OrderLeg(
                label=f"SELL ATM {int(atm_strike)} CE ({expiry})",
                security_id=str(atm_row["ce_security_id"]),
                trading_symbol=f"{underlying} {int(atm_strike)} CE {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type="SELL",
                quantity=quantity,
                order_type=order_type,
                product_type="INTRADAY",
                price=float(ce_price),
                role="primary",
            ),
            OrderLeg(
                label=f"SELL ATM {int(atm_strike)} PE ({expiry})",
                security_id=str(atm_row["pe_security_id"]),
                trading_symbol=f"{underlying} {int(atm_strike)} PE {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type="SELL",
                quantity=quantity,
                order_type=order_type,
                product_type="INTRADAY",
                price=float(pe_price),
                role="primary",
            ),
        ]

        if p.get("hedge_enabled"):
            hedge_target = float(p.get("hedge_premium_target") or 0)
            for option_type, price_col, sid_col in (("CE", "ce_ltp", "ce_security_id"), ("PE", "pe_ltp", "pe_security_id")):
                best_strike, best_row = find_strike_by_nearest_premium(
                    chain_df, strikes, atm_index, option_type, price_col, sid_col, hedge_target, include_start=False,
                )
                if best_row is None:
                    # Hedge was requested but no valid candidate strike was
                    # found this pass — never go live naked when a hedge
                    # was asked for; skip entry and retry next poll.
                    return None
                legs.append(
                    OrderLeg(
                        label=f"HEDGE BUY {int(best_strike)} {option_type} ({expiry})",
                        security_id=str(best_row[sid_col]),
                        trading_symbol=f"{underlying} {int(best_strike)} {option_type} {expiry}",
                        exchange_segment=meta["option_segment"],
                        transaction_type="BUY",
                        quantity=quantity,
                        order_type=order_type,
                        product_type="INTRADAY",
                        price=float(best_row[price_col]),
                        role="hedge",
                    )
                )

        return legs

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        """Whole-position exits only: close time, and the combined-premium
        target. Per-leg stop-loss lives in `evaluate_leg_exits` instead —
        it may need to close just one leg, which this method can't express."""
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return False

        close_time = _parse_hhmm(p["close_time"])
        if _now_ist().time() >= close_time:
            return True

        leg_state = open_run_notes.get("leg_state") or {}
        primary_legs = [leg for leg in legs if leg.get("role") == "primary"]
        open_primary = currently_open_legs(primary_legs, leg_state)
        if not open_primary:
            # Nothing left on the sell side (both legs already closed via
            # per-leg SL) — safety net in case a hedge was somehow left
            # behind; normally evaluate_leg_exits already closed it too.
            return True

        entry_combined = sum(float(leg["price"]) for leg in primary_legs)
        if not entry_combined:
            return False

        securities_by_segment: dict[str, list[int]] = {}
        for leg in open_primary:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

        current_total = 0.0
        for leg in open_primary:
            quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
            if quote is None:
                return False  # can't get a fresh quote this pass — don't guess
            current_total += float(quote.get("last_price", 0))

        target_pct = float(p.get("target_pct", 80))
        change_pct = ((current_total - entry_combined) / entry_combined) * 100
        return change_pct <= -target_pct

    def evaluate_leg_exits(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        leg_state = open_run_notes.get("leg_state") or {}
        leg_by_sid = {str(leg["security_id"]): leg for leg in legs}
        primary_legs = [leg for leg in legs if leg.get("role") == "primary"]
        open_primary = currently_open_legs(primary_legs, leg_state)
        if not open_primary:
            return None

        securities_by_segment: dict[str, list[int]] = {}
        for leg in open_primary:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

        sl_pct = float(p.get("leg_stop_loss_pct", 25))
        to_close_sids: list[str] = []
        for leg in open_primary:
            sid = str(leg["security_id"])
            quote = quotes.get((leg["exchange_segment"], sid))
            if quote is None:
                continue  # can't get a fresh quote this pass — don't guess this leg
            current_price = float(quote.get("last_price", 0))
            entry_price = float(leg["price"])
            state = _leg_state(sid, leg_state)

            if state["sl_moved_to_cost"]:
                hit = current_price >= entry_price  # trailed to breakeven after the other leg's SL
            else:
                hit = entry_price > 0 and ((current_price - entry_price) / entry_price) * 100 >= sl_pct

            if hit:
                to_close_sids.append(sid)

        if not to_close_sids:
            return None

        # Close each hit leg's own-side hedge alongside it — a hedge only
        # exists to protect its matching sold leg.
        closing_sides = {_side_of(leg_by_sid[sid]) for sid in to_close_sids}
        for leg in legs:
            if leg.get("role") != "hedge" or _side_of(leg) not in closing_sides:
                continue
            sid = str(leg["security_id"])
            if _leg_state(sid, leg_state)["status"] == "open":
                to_close_sids.append(sid)

        leg_state_patch: dict[str, Any] = {}
        remaining_primary = [leg for leg in open_primary if str(leg["security_id"]) not in to_close_sids]
        if len(remaining_primary) == 1:
            # Exactly one sold leg survives this pass — trail its stop to
            # its own entry price (cost) as required.
            survivor_sid = str(remaining_primary[0]["security_id"])
            leg_state_patch[survivor_sid] = {"sl_moved_to_cost": True}

        return {"close_security_ids": to_close_sids, "leg_state_patch": leg_state_patch}
