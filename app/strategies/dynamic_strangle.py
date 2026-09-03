"""Dynamic Strangle with UP/DOWN Adjustments.

An intraday short strangle (one SELL CE + one SELL PE) entered `base_distance_points`
away from spot on each side, that keeps itself centered as spot moves by
adjusting the *untested* leg first and only fully resetting once spot
actually reaches the *tested* leg's strike. Balu's spec, paraphrased:

  - The user supplies only one distance number, `base_distance_points`.
    Everything else is derived from it:
      adjustment_distance = base_distance_points / 2
      fresh_strangle_distance = base_distance_points / 4
    Both derived distances are then snapped to the nearest strike actually
    available on the live option chain for the chosen index/expiry — that
    chain *is* the index's real strike interval (50 for NIFTY, 100 for
    BANKNIFTY/SENSEX, etc.), so there is no separate hardcoded
    interval table to keep in sync per index.
  - Entry: CE = nearest(spot + base_distance), PE = nearest(spot - base_distance).
  - UP move (spot rising toward CE): once spot reaches
    `CE_strike - adjustment_distance`, the PE leg (untested side) is
    replaced — new PE = nearest(old PE + adjustment_distance) — while CE
    stays exactly where it is. This can fire at most once per CE leg
    (flagged via `leg_state[ce_sid]["adjusted"]`), which is also all the
    formula ever needs: adjustment_distance is exactly half of
    base_distance, so a second UP step from the same CE would already be
    at/past the CE strike itself, i.e. a reset (below), not a second
    adjustment.
  - UP reset: once spot actually reaches (or passes) the CE strike, both
    legs are closed and a brand new strangle is opened centered on the
    *current live spot* (not the old CE strike — a real market move can
    gap straight past CE by more than one tick between polls, and
    recentering on live spot lets one reset catch up in a single step
    regardless of gap size): new CE = nearest(spot + fresh_distance),
    new PE = nearest(spot - fresh_distance).
  - DOWN move / DOWN reset: the exact mirror image — spot falling toward
    PE adjusts the CE leg first (`leg_state[pe_sid]["adjusted"]`), then
    resets both once spot actually reaches the PE strike.
  - Reset always takes priority over an adjustment at the same poll (a
    big move can jump straight past the adjustment threshold to the
    strike itself, or beyond, in one tick).
  - Genuinely intraday, unlike Iron Condor Rolling / Iron Fly with
    Adjustments: every leg is `product_type="INTRADAY"`, one entry per
    day, and the whole position (plus any adjustments/resets) is force
    -closed daily at `end_time` (2:45pm by default) regardless of expiry
    — never carried to the next trading day. The next day starts a
    completely fresh strangle off that day's own opening spot.
  - Optional combined daily stop-loss/target (fixed rupees, live
    mark-to-market across whatever's open plus realized P&L so far today)
    can close the whole position early — same model as
    `ThreePairRollingStrategy`, the other genuinely-intraday strategy here.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_quotes, fetch_spot_price, get_lot_size
from app.strategies.base import (
    OrderLeg,
    Strategy,
    StrategyContext,
    currently_open_legs,
    leg_option_type,
    leg_pnl,
    leg_strike,
    resolve_order_type,
)

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "00:00").split(":")
    return dt_time(int(hour), int(minute))


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


class DynamicStrangleStrategy(Strategy):
    name = "Dynamic Strangle with Adjustments"
    description = (
        "An intraday short strangle (one SELL CE + one SELL PE), each entered a configured Base "
        "Distance from spot. As spot moves toward one side, the untested side is adjusted in first "
        "(by half the base distance); only once spot actually reaches the tested strike are both "
        "legs closed and a fresh strangle opened around current spot (at a quarter of the base "
        "distance). Strikes are always snapped to the nearest strike really available on the live "
        "chain, so the same base distance behaves correctly on NIFTY, BANKNIFTY, and SENSEX without "
        "any separate per-index setting. Force-closes daily at End Time — never carried overnight."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "expiry_type": "weekly",  # UI filter only ("weekly" | "monthly") — narrows the expiry dropdown
        "lots": 1,
        "start_time": "09:20",  # daily window during which a first entry may happen
        "end_time": "14:45",  # daily entry-window end AND the daily force-close time (every day — intraday)
        "base_distance_points": 2000,  # only distance the user sets — adjustment = /2, fresh strangle = /4
        "daily_stop_loss": 10000,  # rupees, combined across both legs; 0 disables
        "daily_target": 15000,  # rupees, combined across both legs; 0 disables
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    # --- entry: CE/PE at spot +/- base_distance_points ---

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

        base_distance = float(p.get("base_distance_points") or 0)
        if base_distance <= 0:
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
        ce_strike = _nearest_strike(strikes, spot + base_distance)
        pe_strike = _nearest_strike(strikes, spot - base_distance)
        if ce_strike == pe_strike:
            return None  # base distance too small relative to the available strike spacing

        def _row(strike: float):
            matches = chain_df[chain_df["strike"] == strike]
            if matches.empty:
                return None
            row = matches.iloc[0]
            if not row.get("ce_security_id") or not row.get("pe_security_id") or row.get("ce_ltp") is None or row.get("pe_ltp") is None:
                return None
            return row

        legs: list[OrderLeg] = []
        lot_size: int | None = None
        leg_specs = (
            (ce_strike, "CE", "ce_ltp", "ce_security_id"),
            (pe_strike, "PE", "pe_ltp", "pe_security_id"),
        )
        for strike, option_type, price_col, sid_col in leg_specs:
            row = _row(strike)
            if row is None:
                return None  # never enter one-legged — need both legs or none

            if lot_size is None:
                lot_size = get_lot_size(security_id=row[sid_col]) or 75
            quantity = lot_size * int(p["lots"])

            legs.append(OrderLeg(
                label=f"SELL {int(strike)} {option_type} ({expiry})",
                security_id=str(row[sid_col]),
                trading_symbol=f"{underlying} {int(strike)} {option_type} {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type="SELL",
                quantity=quantity,
                order_type=order_type,
                product_type="INTRADAY",  # daily square-off, never carried overnight
                price=float(row[price_col]),
                role="primary",
            ))

        return legs

    # --- whole-run exit: daily end_time close + combined stop-loss/target ---

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return False

        end_time = _parse_hhmm(p["end_time"])
        if _now_ist().time() >= end_time:
            return True  # daily square-off — every day, not just expiry day

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

    # --- adjustment / reset: untested side moves first, both reset once the tested strike is reached ---

    def evaluate_rolls(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        p = {**self.default_params, **ctx.params}
        order_type = resolve_order_type(p)
        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None
        expiry = p.get("expiry")
        if not expiry:
            return None

        base_distance = float(p.get("base_distance_points") or 0)
        if base_distance <= 0:
            return None
        adjustment_distance = base_distance / 2
        fresh_distance = base_distance / 4

        leg_state = open_run_notes.get("leg_state") or {}
        open_now = [leg for leg in currently_open_legs(legs, leg_state) if leg.get("role") == "primary"]

        ce_legs = [leg for leg in open_now if leg_option_type(leg) == "CE"]
        pe_legs = [leg for leg in open_now if leg_option_type(leg) == "PE"]
        if len(ce_legs) != 1 or len(pe_legs) != 1:
            return None  # not a clean single-CE/single-PE strangle right now — don't guess, leave it alone

        ce = ce_legs[0]
        pe = pe_legs[0]
        ce_strike = leg_strike(ce)
        pe_strike = leg_strike(pe)
        if ce_strike is None or pe_strike is None:
            return None

        ce_sid = str(ce["security_id"])
        pe_sid = str(pe["security_id"])
        ce_state = leg_state.get(ce_sid) or {}
        pe_state = leg_state.get(pe_sid) or {}

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        # Reset always takes priority over an adjustment — a fast enough
        # move can jump straight past the adjustment threshold to (or
        # beyond) the strike itself between two polls.
        up_reset = spot >= ce_strike
        down_reset = spot <= pe_strike
        up_adjust = not up_reset and spot >= (ce_strike - adjustment_distance) and not ce_state.get("adjusted")
        down_adjust = not down_reset and spot <= (pe_strike + adjustment_distance) and not pe_state.get("adjusted")

        if not (up_reset or down_reset or up_adjust or down_adjust):
            return None

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

        quantity = ce["quantity"]

        if up_reset or down_reset:
            # Recenter both legs on current live spot — not the old
            # CE/PE strike — so one reset always catches up in a single
            # step, however far spot has already moved past the trigger.
            new_ce_strike = _nearest_strike(strikes_avail, spot + fresh_distance)
            new_pe_strike = _nearest_strike(strikes_avail, spot - fresh_distance)
            if new_ce_strike == new_pe_strike:
                return None  # fresh distance too small relative to available strike spacing — don't guess
            ce_row = _row(new_ce_strike)
            pe_row = _row(new_pe_strike)
            if ce_row is None or pe_row is None:
                return None
            new_legs = [
                OrderLeg(
                    label=f"RESET SELL {int(new_ce_strike)} CE ({expiry})", security_id=str(ce_row["ce_security_id"]),
                    trading_symbol=f"{underlying} {int(new_ce_strike)} CE {expiry}", exchange_segment=meta["option_segment"],
                    transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                    price=float(ce_row["ce_ltp"]), role="primary",
                ),
                OrderLeg(
                    label=f"RESET SELL {int(new_pe_strike)} PE ({expiry})", security_id=str(pe_row["pe_security_id"]),
                    trading_symbol=f"{underlying} {int(new_pe_strike)} PE {expiry}", exchange_segment=meta["option_segment"],
                    transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                    price=float(pe_row["pe_ltp"]), role="primary",
                ),
            ]
            return {"rolls": [{"close_security_ids": [ce_sid, pe_sid], "new_legs": new_legs}]}

        if up_adjust:
            # Spot moving toward CE -> CE stays put, PE (untested) moves
            # up by adjustment_distance.
            new_pe_strike = _nearest_strike(strikes_avail, pe_strike + adjustment_distance)
            if new_pe_strike == ce_strike:
                return None  # would collide with the still-open CE strike — don't guess
            pe_row = _row(new_pe_strike)
            if pe_row is None:
                return None
            new_leg = OrderLeg(
                label=f"ADJUST SELL {int(new_pe_strike)} PE ({expiry})", security_id=str(pe_row["pe_security_id"]),
                trading_symbol=f"{underlying} {int(new_pe_strike)} PE {expiry}", exchange_segment=meta["option_segment"],
                transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                price=float(pe_row["pe_ltp"]), role="primary",
            )
            return {"rolls": [{
                "close_security_ids": [pe_sid],
                "new_legs": [new_leg],
                # Flag the (unchanged, still-open) CE leg so this same
                # boundary doesn't adjust the PE side again.
                "leg_state_patch": {ce_sid: {**ce_state, "adjusted": True}},
            }]}

        # down_adjust: spot moving toward PE -> PE stays put, CE (untested) moves down.
        new_ce_strike = _nearest_strike(strikes_avail, ce_strike - adjustment_distance)
        if new_ce_strike == pe_strike:
            return None  # would collide with the still-open PE strike — don't guess
        ce_row = _row(new_ce_strike)
        if ce_row is None:
            return None
        new_leg = OrderLeg(
            label=f"ADJUST SELL {int(new_ce_strike)} CE ({expiry})", security_id=str(ce_row["ce_security_id"]),
            trading_symbol=f"{underlying} {int(new_ce_strike)} CE {expiry}", exchange_segment=meta["option_segment"],
            transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
            price=float(ce_row["ce_ltp"]), role="primary",
        )
        return {"rolls": [{
            "close_security_ids": [ce_sid],
            "new_legs": [new_leg],
            # Flag the (unchanged, still-open) PE leg so this same
            # boundary doesn't adjust the CE side again.
            "leg_state_patch": {pe_sid: {**pe_state, "adjusted": True}},
        }]}
