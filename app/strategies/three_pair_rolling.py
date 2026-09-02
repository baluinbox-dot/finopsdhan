"""Dynamic T-M-B 3-Pair Rolling Strategy.

Sells three short straddles (CE+PE) at strikes exactly one strike-gap
apart — Top, Middle, Bottom — at a configured start time, and keeps a
continuously-maintained 3-strike window around spot for the rest of the
day. T/M/B are *dynamic roles*, not fixed identities: whichever strike is
currently highest is "T", currently lowest is "B", regardless of which
physical position has held it or for how long.

When spot reaches (or passes) the current B strike, the window shifts
down: close the current T pair, open a new pair one gap below the current
B, and relabel — new T = old M, new M = old B, new B = the newly-opened
strike. Symmetrically, spot reaching the current T strike shifts the
window up: close B, open a new pair one gap above T, new T = the new
strike, new M = old T, new B = old M. Only ever one pair is closed and one
opened per shift; the middle position is *never* touched by a shift, it
just gets relabeled. A big enough spot move triggers more than one shift
in the same pass — each computed off the *previous* shift's resulting
window, never off a stale spot-derived target shared across shifts, so
two shifts in the same pass can never target the same strike.

A single daily stop-loss/target (live mark-to-market: realized P&L from
completed shifts so far *plus* unrealized P&L on whatever's still open,
re-checked every poll) closes every pair and stops the strategy for the
rest of the day the moment either is breached — as does the configured
end time. Manual Close reuses the existing generic "Close Now" path
unchanged. One entry per day, same `ctx.today_run_count` convention as
every other strategy here.

An optional hedge (one CE buy + one PE buy, picked by nearest live premium
to a target price, e.g. Rs 5 or Rs 10) protects the *combined* exposure of
all three pairs at once — sized at `lots * 3`, since three pairs each
sell `lots` on both sides. The hedge is bought once at entry and never
rolls with the T/M/B window (`evaluate_rolls` only ever touches
`role == "primary"` legs) — it sits fixed for the whole day and is closed,
along with everything else, by the existing whole-position close path
(`_close_open_run` in the engine), whether that's the daily SL/target, end
time, or a manual Close Now.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import (
    UNDERLYINGS,
    fetch_chain_df,
    fetch_quotes,
    fetch_spot_price,
    find_strike_by_nearest_premium,
    get_lot_size,
)
from app.strategies.base import OrderLeg, Strategy, StrategyContext, currently_open_legs, leg_pnl, resolve_order_type

IST = ZoneInfo("Asia/Kolkata")

# Safety cap on how many shifts one evaluate_rolls call will produce, in
# case of a wildly gappy chain/spot value — a normal single poll never
# needs more than a couple even after a large intraday jump.
_MAX_SHIFTS_PER_PASS = 6


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


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


class ThreePairRollingStrategy(Strategy):
    name = "Dynamic T-M-B 3-Pair Rolling Strategy"
    description = (
        "Sells three short straddles (CE+PE) forming a Top/Middle/Bottom window one "
        "strike-gap apart, at a fixed start time. As spot moves, the window shifts: "
        "reaching the current Bottom closes Top and opens a new Bottom one gap below; "
        "reaching the current Top closes Bottom and opens a new Top one gap above. T/M/B "
        "are roles, not fixed strikes — the middle position is never touched by a shift, "
        "only relabeled. A single daily stop-loss/target (live mark-to-market across all "
        "three legs of the window) closes everything and stops the strategy for the rest "
        "of the day, as does the configured end time. One entry per day."
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
        "hedge_enabled": False,
        "hedge_premium_target": 5,  # buy the closest-premium CE/PE hedge to this price
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}
        order_type = resolve_order_type(p)

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
        m_strike = _nearest_strike(strikes, spot)

        targets = {
            "T": _nearest_strike(strikes, m_strike + gap),
            "M": m_strike,
            "B": _nearest_strike(strikes, m_strike - gap),
        }

        legs: list[OrderLeg] = []
        lot_size: int | None = None
        for role, strike in targets.items():
            row_matches = chain_df[chain_df["strike"] == strike]
            if row_matches.empty:
                return None  # never enter short-handed — need all 3 legs of the window or none
            row = row_matches.iloc[0]
            if not row.get("ce_security_id") or not row.get("pe_security_id") or row.get("ce_ltp") is None or row.get("pe_ltp") is None:
                return None

            if lot_size is None:
                lot_size = get_lot_size(security_id=row["ce_security_id"]) or 75
            quantity = lot_size * int(p["lots"])

            legs.append(OrderLeg(
                label=f"{role} SELL {int(strike)} CE ({expiry})",
                security_id=str(row["ce_security_id"]),
                trading_symbol=f"{underlying} {int(strike)} CE {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type="SELL",
                quantity=quantity,
                order_type=order_type,
                product_type="INTRADAY",
                price=float(row["ce_ltp"]),
                role="primary",
            ))
            legs.append(OrderLeg(
                label=f"{role} SELL {int(strike)} PE ({expiry})",
                security_id=str(row["pe_security_id"]),
                trading_symbol=f"{underlying} {int(strike)} PE {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type="SELL",
                quantity=quantity,
                order_type=order_type,
                product_type="INTRADAY",
                price=float(row["pe_ltp"]),
                role="primary",
            ))

        if p.get("hedge_enabled"):
            # One CE hedge + one PE hedge cover all three pairs' combined
            # exposure at once (not one hedge per pair) — sized at 3x the
            # per-pair lot count. Picked once, from the initial ATM, and
            # never touched again today (see module docstring).
            atm_index = strikes.index(m_strike)
            hedge_target = float(p.get("hedge_premium_target") or 0)
            hedge_quantity = (lot_size or 0) * int(p["lots"]) * 3
            for option_type, price_col, sid_col in (("CE", "ce_ltp", "ce_security_id"), ("PE", "pe_ltp", "pe_security_id")):
                best_strike, best_row = find_strike_by_nearest_premium(
                    chain_df, strikes, atm_index, option_type, price_col, sid_col, hedge_target, include_start=False,
                )
                if best_row is None:
                    # Hedge was requested but no valid candidate strike was
                    # found this pass — never go live naked when a hedge
                    # was asked for; skip entry and retry next poll.
                    return None
                legs.append(OrderLeg(
                    label=f"HEDGE BUY {int(best_strike)} {option_type} ({expiry})",
                    security_id=str(best_row[sid_col]),
                    trading_symbol=f"{underlying} {int(best_strike)} {option_type} {expiry}",
                    exchange_segment=meta["option_segment"],
                    transaction_type="BUY",
                    quantity=hedge_quantity,
                    order_type=order_type,
                    product_type="INTRADAY",
                    price=float(best_row[price_col]),
                    role="hedge",
                ))

        return legs

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        """Whole-position exit: end time, or the daily stop-loss/target on
        live mark-to-market P&L across the whole window (realized so far,
        plus unrealized on whatever's currently open)."""
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return False

        end_time = _parse_hhmm(p["end_time"])
        if _now_ist().time() >= end_time:
            return True

        leg_state = open_run_notes.get("leg_state") or {}
        open_legs = currently_open_legs(legs, leg_state)
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
        order_type = resolve_order_type(p)
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

        # Currently-open legs grouped by strike — the window should always
        # be exactly 3 strikes (B, M, T ascending), each with a CE+PE pair.
        # Hedge legs are deliberately excluded: they're bought once at
        # entry and never roll with the window (see module docstring).
        # currently_open_legs dedupes each security_id to its one genuinely
        # -open history entry first — this strategy's whole premise is
        # spot oscillating back and forth, so a strike it already closed
        # earlier today is routinely revisited later the same run.
        groups: dict[float, list[dict]] = {}
        for leg in currently_open_legs(legs, leg_state):
            if leg.get("role") != "primary":
                continue
            strike = _strike_of(leg)
            if strike is None:
                continue
            groups.setdefault(strike, []).append(leg)

        open_strikes = sorted(groups.keys())
        if len(open_strikes) != 3:
            return None  # not a clean B/M/T window right now — don't guess, leave it alone

        b, m, t = open_strikes
        window: dict[float, list[dict]] = dict(groups)  # simulated state, updated as shifts are planned

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        if b < spot < t:
            return None  # comfortably inside the window — skip the chain fetch entirely

        chain_df, _ = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None
        strikes_avail = sorted(chain_df["strike"].tolist())

        def _row(strike: float):
            matches = chain_df[chain_df["strike"] == strike]
            if matches.empty:
                return None
            row = matches.iloc[0]
            if not row.get("ce_security_id") or not row.get("pe_security_id") or row.get("ce_ltp") is None or row.get("pe_ltp") is None:
                return None
            return row

        rolls: list[dict[str, Any]] = []

        for _ in range(_MAX_SHIFTS_PER_PASS):
            if spot <= b:
                target = _nearest_strike(strikes_avail, b - gap)
                closing_strike, role = t, "B"
            elif spot >= t:
                target = _nearest_strike(strikes_avail, t + gap)
                closing_strike, role = b, "T"
            else:
                break  # spot has settled back inside the (possibly already-shifted) window

            if target in window:
                # Structurally shouldn't happen (target is always exactly
                # `gap` beyond the current boundary, distinct from the
                # other two open strikes) — if it does, something's off
                # with the chain data; stop shifting rather than guess.
                break
            row = _row(target)
            if row is None:
                break  # no valid contract at the target strike — stop here, retry next poll

            closing_legs = window.pop(closing_strike)
            quantity = closing_legs[0]["quantity"]
            new_legs = [
                OrderLeg(
                    label=f"{role} SELL {int(target)} CE ({expiry})", security_id=str(row["ce_security_id"]),
                    trading_symbol=f"{underlying} {int(target)} CE {expiry}", exchange_segment=meta["option_segment"],
                    transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                    price=float(row["ce_ltp"]), role="primary",
                ),
                OrderLeg(
                    label=f"{role} SELL {int(target)} PE ({expiry})", security_id=str(row["pe_security_id"]),
                    trading_symbol=f"{underlying} {int(target)} PE {expiry}", exchange_segment=meta["option_segment"],
                    transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                    price=float(row["pe_ltp"]), role="primary",
                ),
            ]
            rolls.append({
                "close_security_ids": [str(leg["security_id"]) for leg in closing_legs],
                "new_legs": new_legs,
            })
            window[target] = [asdict(leg) for leg in new_legs]

            # Re-derive B/M/T from the updated window for the next loop
            # check (handles a spot move spanning more than one gap).
            b, m, t = sorted(window.keys())

        if not rolls:
            return None
        return {"rolls": rolls}
