"""3-Pair Dynamic Rolling Strategy.

Sells three independent ATM-ish short straddles ("pairs" — FIN1/FIN2/FIN3),
staggered one strike-gap apart, at a configured start time. As spot moves,
each pair rolls independently: once spot has moved two strike-gaps away
from a pair's current strike, that pair closes its CE+PE and reopens a
fresh CE+PE one strike-gap beyond the new spot, in the direction of the
move — its own pair identity (FIN1/2/3) never changes, only which strike
it currently holds.

A strike that's ever been used for a *new* entry by one pair today can't
be used for a new entry by a *different* pair the same day (existing
positions are unaffected) — checked against every leg any pair has ever
held today, not just what's currently open, which is why rolled-away legs
are kept in `legs_planned["legs"]` rather than discarded.

A single daily stop-loss/target (live mark-to-market: realized P&L from
completed rolls so far *plus* unrealized P&L on whatever's still open,
re-checked every poll) closes every pair and stops the strategy for the
rest of the day the moment either is breached — as does the configured
end time. One entry per day, same `ctx.today_run_count` convention as
every other strategy here.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_quotes, fetch_spot_price, get_lot_size
from app.strategies.base import OrderLeg, Strategy, StrategyContext, leg_pnl

IST = ZoneInfo("Asia/Kolkata")

PAIR_IDS = ("FIN1", "FIN2", "FIN3")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "00:00").split(":")
    return dt_time(int(hour), int(minute))


def _strike_of(leg: dict) -> float | None:
    """Strike parsed from the trading_symbol this strategy builds itself
    (`"{underlying} {strike} {CE|PE} {expiry}"`) — safe only because every
    UNDERLYINGS key is a single space-free token."""
    tokens = (leg.get("trading_symbol") or "").split()
    if len(tokens) < 2:
        return None
    try:
        return float(tokens[1])
    except ValueError:
        return None


def _leg_state(sid: str, leg_state: dict) -> dict:
    return {"status": "open", **(leg_state.get(sid) or {})}


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


class ThreePairRollingStrategy(Strategy):
    name = "3-Pair Dynamic Rolling Strategy"
    description = (
        "Sells three independent ATM-ish short straddles (FIN1/FIN2/FIN3), one strike-gap "
        "apart, at a fixed start time. Each pair rolls independently as spot moves two "
        "strike-gaps away from its current strike — closing and reopening one gap beyond "
        "the new spot — while keeping its own identity. A strike already used for a new "
        "entry by one pair can't be reused by another pair the same day. A single daily "
        "stop-loss/target (live mark-to-market across all three pairs) closes everything "
        "and stops the strategy for the rest of the day, as does the configured end time. "
        "One entry per day."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "lots": 1,
        "start_time": "09:20",
        "end_time": "14:45",
        "strike_gap": 50,
        "daily_stop_loss": 10000,
        "daily_target": 15000,
    }

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}

        now_ist = _now_ist()
        start_time = _parse_hhmm(p["start_time"])
        end_time = _parse_hhmm(p["end_time"])
        if not (start_time <= now_ist.time() < end_time):
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

        gap = float(p.get("strike_gap") or 0)
        if gap <= 0:
            return None

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        atm_strike = _nearest_strike(strikes, spot)

        # FIN1 = ATM + gap, FIN2 = ATM, FIN3 = ATM - gap.
        targets = {
            "FIN1": _nearest_strike(strikes, atm_strike + gap),
            "FIN2": atm_strike,
            "FIN3": _nearest_strike(strikes, atm_strike - gap),
        }

        legs: list[OrderLeg] = []
        lot_size: int | None = None
        for pair_id, strike in targets.items():
            row_matches = chain_df[chain_df["strike"] == strike]
            if row_matches.empty:
                return None  # never enter short-handed — need all 3 pairs or none
            row = row_matches.iloc[0]
            if not row.get("ce_security_id") or not row.get("pe_security_id") or row.get("ce_ltp") is None or row.get("pe_ltp") is None:
                return None

            if lot_size is None:
                lot_size = get_lot_size(security_id=row["ce_security_id"]) or 75
            quantity = lot_size * int(p["lots"])

            legs.append(OrderLeg(
                label=f"{pair_id} SELL {int(strike)} CE ({expiry})",
                security_id=str(row["ce_security_id"]),
                trading_symbol=f"{underlying} {int(strike)} CE {expiry}",
                exchange_segment="NSE_FNO",
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                product_type="INTRADAY",
                price=float(row["ce_ltp"]),
                role="primary",
                pair_id=pair_id,
            ))
            legs.append(OrderLeg(
                label=f"{pair_id} SELL {int(strike)} PE ({expiry})",
                security_id=str(row["pe_security_id"]),
                trading_symbol=f"{underlying} {int(strike)} PE {expiry}",
                exchange_segment="NSE_FNO",
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                product_type="INTRADAY",
                price=float(row["pe_ltp"]),
                role="primary",
                pair_id=pair_id,
            ))

        return legs

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        """Whole-position exit: end time, or the daily stop-loss/target on
        live mark-to-market P&L across every pair (realized so far, plus
        unrealized on whatever's currently open)."""
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return False

        end_time = _parse_hhmm(p["end_time"])
        if _now_ist().time() >= end_time:
            return True

        leg_state = open_run_notes.get("leg_state") or {}
        open_legs = [leg for leg in legs if _leg_state(str(leg["security_id"]), leg_state)["status"] == "open"]
        if not open_legs:
            return False

        realized_so_far = float(open_run_notes.get("realized_pnl_so_far") or 0)

        securities_by_segment: dict[str, list[int]] = {}
        for leg in open_legs:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

        unrealized = 0.0
        for leg in open_legs:
            quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
            if quote is None:
                return False  # can't get a fresh quote for everything this pass — don't guess the daily total
            unrealized += leg_pnl(leg, float(quote.get("last_price", 0)))

        total_pnl = realized_so_far + unrealized

        daily_sl = float(p.get("daily_stop_loss") or 0)
        if daily_sl and total_pnl <= -daily_sl:
            return True

        daily_target = float(p.get("daily_target") or 0)
        if daily_target and total_pnl >= daily_target:
            return True

        return False

    def evaluate_rolls(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        gap = float(p.get("strike_gap") or 0)
        if gap <= 0:
            return None

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None
        expiry = p.get("expiry")
        if not expiry:
            return None

        leg_state = open_run_notes.get("leg_state") or {}

        # Currently-open legs, grouped by pair.
        open_by_pair: dict[str, dict] = {}
        for leg in legs:
            pair_id = leg.get("pair_id")
            if not pair_id or _leg_state(str(leg["security_id"]), leg_state)["status"] != "open":
                continue
            strike = _strike_of(leg)
            if strike is None:
                continue
            bucket = open_by_pair.setdefault(pair_id, {"strike": strike, "legs": []})
            bucket["legs"].append(leg)

        if not open_by_pair:
            return None

        # Every strike any pair has ever taken a new entry at today (open
        # or since rolled away) — the unique-spot-per-day rule looks at
        # this full history, not just what's currently open.
        owner_of_strike: dict[float, str] = {}
        for leg in legs:
            pair_id = leg.get("pair_id")
            strike = _strike_of(leg)
            if pair_id and strike is not None:
                owner_of_strike[strike] = pair_id

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        # Which pairs actually need to roll this pass — check before
        # fetching the chain, so a quiet market does zero extra API calls.
        due = []
        for pair_id, info in open_by_pair.items():
            current_strike = info["strike"]
            if spot <= current_strike - 2 * gap:
                due.append((pair_id, info, spot - gap))
            elif spot >= current_strike + 2 * gap:
                due.append((pair_id, info, spot + gap))
        if not due:
            return None

        chain_df, _ = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None
        strikes = sorted(chain_df["strike"].tolist())

        rolls = []
        for pair_id, info, raw_target in due:
            target_strike = _nearest_strike(strikes, raw_target)

            owner = owner_of_strike.get(target_strike)
            if owner is not None and owner != pair_id:
                # Unique-spot-per-day rule blocks this roll — skip it,
                # keep the pair right where it is, and re-check next poll
                # (spot will have moved on by then; never go flat just
                # because one specific roll was blocked).
                continue

            row_matches = chain_df[chain_df["strike"] == target_strike]
            if row_matches.empty:
                continue
            row = row_matches.iloc[0]
            if not row.get("ce_security_id") or not row.get("pe_security_id") or row.get("ce_ltp") is None or row.get("pe_ltp") is None:
                continue

            quantity = info["legs"][0]["quantity"] if info["legs"] else int(p["lots"]) * 75
            close_ids = [str(leg["security_id"]) for leg in info["legs"]]

            new_legs = [
                OrderLeg(
                    label=f"{pair_id} ROLL SELL {int(target_strike)} CE ({expiry})",
                    security_id=str(row["ce_security_id"]),
                    trading_symbol=f"{underlying} {int(target_strike)} CE {expiry}",
                    exchange_segment="NSE_FNO", transaction_type="SELL", quantity=quantity,
                    order_type="LIMIT", product_type="INTRADAY", price=float(row["ce_ltp"]),
                    role="primary", pair_id=pair_id,
                ),
                OrderLeg(
                    label=f"{pair_id} ROLL SELL {int(target_strike)} PE ({expiry})",
                    security_id=str(row["pe_security_id"]),
                    trading_symbol=f"{underlying} {int(target_strike)} PE {expiry}",
                    exchange_segment="NSE_FNO", transaction_type="SELL", quantity=quantity,
                    order_type="LIMIT", product_type="INTRADAY", price=float(row["pe_ltp"]),
                    role="primary", pair_id=pair_id,
                ),
            ]
            rolls.append({"close_security_ids": close_ids, "new_legs": new_legs})

        if not rolls:
            return None
        return {"rolls": rolls}
