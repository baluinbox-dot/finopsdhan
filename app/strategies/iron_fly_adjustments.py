"""Iron Fly with Adjustments.

A short Iron Butterfly (CES/PES sold at the *same* ATM strike, with a CEB
and PEB wing bought on each side) that adjusts by **scaling in** on the
untested side whenever the market reaches the tested side's wing, instead
of rolling or closing anything. Balu's spec, paraphrased:

  - Entry legs, in the app's usual CE/PE Buy/Sell naming:
      CES = SELL ATM CE, PES = SELL ATM PE   (same strike, e.g. 24000)
      CEB = BUY  CE wing, PEB = BUY  PE wing (each wing's distance from
      ATM is a user-supplied number of points, entered independently per
      side — a 100-point call wing and a 300-point put wing is a valid,
      deliberately asymmetric fly, not a mistake).
  - The configure page shows the live ATM CE+PE combined premium purely
    as a reference for picking those wing distances — it is *not* an
    entry trigger; entry still fires on the configured time window
    regardless of the premium's value.
  - Adjustment ("scale-in"): if spot reaches CEB, the CE side is *not*
    touched — instead, one more PES is added, riding the existing PEB
    for protection rather than buying a fresh wing. Symmetrically, spot
    reaching PEB adds one more CES, riding the existing CEB. The new
    leg's strike is `ATM + <side>_scale_in_offset_points` (signed —
    positive lands above ATM, negative below, 0 lands exactly on ATM,
    i.e. the same strike the original PES/CES already sits at, which is
    the default). Each side scales in **at most once per run** — once
    CEB has triggered a PE-side add, further polls with spot still
    at/beyond that same CEB do not add again (mirrors Iron Condor
    Rolling's `triggered_roll` guard, called `scaled_in` here); CEB/PEB
    themselves never move, so this guard is permanent for the run rather
    than reset by anything.
  - Not intraday — same time model as Iron Condor Rolling. Legs use
    `product_type="MARGIN"` (not "INTRADAY") so the broker doesn't
    auto-square them off overnight. `start_time`/`end_time` bound only
    the *daily entry window* (when a first entry may happen); the
    whole-position force-close happens only on the expiry day itself, at
    `end_time` that day.
  - Stop-loss/target: a single combined check against the whole open
    position (realized P&L so far this run + live mark-to-market on
    everything still open), fixed rupees or % of total premium collected
    across every leg ever entered this run (initial entry plus every
    scale-in add) — same model as Iron Condor Rolling.

The scale-in itself is implemented via the engine's `evaluate_rolls`
hook using `allow_empty_close=True` (no `close_security_ids`) — a roll
whose only effect is opening a brand new leg, without reversing
anything. See `app/engine/runner.py::_apply_rolls`.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_quotes, fetch_spot_price, get_lot_size
from app.strategies.base import OrderLeg, Strategy, StrategyContext, leg_pnl, resolve_order_type

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "00:00").split(":")
    return dt_time(int(hour), int(minute))


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


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


class IronFlyAdjustmentsStrategy(Strategy):
    name = "Iron Fly with Adjustments"
    description = (
        "A short Iron Butterfly (CES/PES sold at the same ATM strike, CEB/PEB wings bought at "
        "independently-set point distances per side), held across multiple days (not intraday) like "
        "Iron Condor Rolling. Adjusts by scaling in rather than rolling: if spot reaches a wing, the "
        "tested side is left alone and one more sell leg is added on the untested side, at a strike you "
        "set as signed points from ATM (0 = the original ATM strike), riding that side's existing wing — "
        "at most once per side per run. A single combined stop-loss/target (fixed rupees or % of total "
        "premium collected) can close it early; otherwise it force-closes on the expiry day itself."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "expiry_type": "weekly",  # UI filter only ("weekly" | "monthly") — narrows the expiry dropdown
        "lots": 1,
        "start_time": "09:20",  # daily window during which a first entry may happen
        "end_time": "14:45",  # daily entry-window end, AND the force-close time on the expiry day itself
        "ce_wing_offset_points": 300,  # CEB strike = ATM + this
        "pe_wing_offset_points": 300,  # PEB strike = ATM - this (independent of the CE side)
        # Where each side's scale-in leg lands, signed points from ATM
        # (positive = above ATM, negative = below, 0 = original ATM strike
        # -- same strike PES/CES already sits at, the pre-existing default
        # behavior). Independent per side, same as the wing offsets.
        "ce_scale_in_offset_points": 0,  # new CES strike = ATM + this (added when PEB is touched)
        "pe_scale_in_offset_points": 0,  # new PES strike = ATM + this (added when CEB is touched)
        "sl_target_mode": "fixed",  # "fixed" (rupees) | "pct" (of total premium collected this run so far)
        "stop_loss_value": 10000,
        "target_value": 15000,
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    # --- entry: CES+PES at ATM, CEB/PEB at independently-set wing offsets ---

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
        try:
            expiry_date = date.fromisoformat(expiry)
        except ValueError:
            return None
        if now_ist.date() > expiry_date:
            return None  # this contract has already expired — never enter it

        ce_wing_offset = float(p.get("ce_wing_offset_points") or 0)
        pe_wing_offset = float(p.get("pe_wing_offset_points") or 0)
        if ce_wing_offset <= 0 or pe_wing_offset <= 0:
            return None  # both wings are required — never enter naked on either side

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
        targets = {
            "CEB": _nearest_strike(strikes, atm_strike + ce_wing_offset),
            "CES": atm_strike,
            "PES": atm_strike,
            "PEB": _nearest_strike(strikes, atm_strike - pe_wing_offset),
        }
        # Never enter with a wing collapsed onto the ATM strike (offset too
        # small relative to the available strike spacing).
        if targets["CEB"] == atm_strike or targets["PEB"] == atm_strike:
            return None

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
            ("CEB", "BUY", "CE", "ce_ltp", "ce_security_id", "CE"),
            ("CES", "SELL", "CE", "ce_ltp", "ce_security_id", "CE"),
            ("PES", "SELL", "PE", "pe_ltp", "pe_security_id", "PE"),
            ("PEB", "BUY", "PE", "pe_ltp", "pe_security_id", "PE"),
        )
        for role, txn, option_type, price_col, sid_col, pair_id in leg_specs:
            strike = targets[role]
            row = _row(strike)
            if row is None:
                return None  # never enter short-handed — need all 4 legs or none

            if lot_size is None:
                lot_size = get_lot_size(security_id=row[sid_col]) or 75
            quantity = lot_size * int(p["lots"])

            legs.append(OrderLeg(
                label=f"{role} {txn} {int(strike)} {option_type} ({expiry})",
                security_id=str(row[sid_col]),
                trading_symbol=f"{underlying} {int(strike)} {option_type} {expiry}",
                exchange_segment=meta["option_segment"],
                transaction_type=txn,
                quantity=quantity,
                order_type=order_type,
                product_type="MARGIN",  # carried forward across days, NOT auto-squared-off intraday by the broker
                price=float(row[price_col]),
                role="primary",
                pair_id=pair_id,
            ))

        return legs

    # --- whole-run exit: expiry-day close + combined stop-loss/target ---
    # Deliberately NOT a daily end_time close — this strategy holds across
    # days, same as Iron Condor Rolling.

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return False

        now_ist = _now_ist()
        expiry = p.get("expiry")
        if expiry:
            try:
                expiry_date = date.fromisoformat(expiry)
            except ValueError:
                expiry_date = None
            if expiry_date is not None:
                if now_ist.date() > expiry_date:
                    return True  # expiry has fully passed — safety catch-up, close immediately regardless of time
                if now_ist.date() == expiry_date and now_ist.time() >= _parse_hhmm(p["end_time"]):
                    return True  # expiry day itself, past the close time

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
                return False  # can't get a fresh quote for everything this pass — don't guess the total
            unrealized += leg_pnl(leg, float(quote.get("last_price", 0)))

        total_pnl = realized_so_far + unrealized

        # Total premium ever collected this run, across every day it's been
        # open (initial entry + every scale-in add) — the running credit
        # base for "pct" mode. Includes closed legs too (none, normally,
        # since this strategy never closes a leg before the whole run ends).
        total_premium_collected = sum(
            float(leg["price"]) * leg["quantity"] if leg["transaction_type"] == "SELL" else -float(leg["price"]) * leg["quantity"]
            for leg in legs
        )

        mode = p.get("sl_target_mode") or "fixed"
        sl_value = float(p.get("stop_loss_value") or 0)
        target_value = float(p.get("target_value") or 0)

        if mode == "pct":
            base = total_premium_collected
            sl_threshold = -(base * sl_value / 100) if sl_value and base > 0 else None
            target_threshold = (base * target_value / 100) if target_value and base > 0 else None
        else:
            sl_threshold = -sl_value if sl_value else None
            target_threshold = target_value if target_value else None

        if sl_threshold is not None and total_pnl <= sl_threshold:
            return True
        if target_threshold is not None and total_pnl >= target_threshold:
            return True

        return False

    # --- adjustment: scale in the untested side, at most once per side ---

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

        leg_state = open_run_notes.get("leg_state") or {}

        def _open_side_legs(pair_id: str, txn: str) -> list[dict]:
            return [
                leg for leg in legs
                if leg.get("pair_id") == pair_id and leg.get("transaction_type") == txn
                and _leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
            ]

        ceb_legs = _open_side_legs("CE", "BUY")
        peb_legs = _open_side_legs("PE", "BUY")
        ces_legs = _open_side_legs("CE", "SELL")
        pes_legs = _open_side_legs("PE", "SELL")
        # Exactly one wing per side at all times (wings never scale in), but
        # at least one sell leg per side (one, or more after a prior scale-in).
        if len(ceb_legs) != 1 or len(peb_legs) != 1 or not ces_legs or not pes_legs:
            return None  # not a clean fly right now — don't guess, leave it alone

        ceb = ceb_legs[0]
        peb = peb_legs[0]
        ceb_strike = _strike_of(ceb)
        peb_strike = _strike_of(peb)
        if ceb_strike is None or peb_strike is None:
            return None

        ceb_sid = str(ceb["security_id"])
        peb_sid = str(peb["security_id"])
        ceb_state = leg_state.get(ceb_sid) or {}
        peb_state = leg_state.get(peb_sid) or {}

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        # Each wing only ever triggers its opposite side's scale-in once —
        # CEB/PEB never move in this strategy, so once flagged this stays
        # flagged for the rest of the run (unlike a roll, there's no new
        # leg to carry a fresh, untriggered flag).
        trigger_ce_side_touched = spot >= ceb_strike and not ceb_state.get("scaled_in")
        trigger_pe_side_touched = spot <= peb_strike and not peb_state.get("scaled_in")
        if not trigger_ce_side_touched and not trigger_pe_side_touched:
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

        rolls: list[dict[str, Any]] = []

        if trigger_ce_side_touched:
            # CE side reached its wing -> the CE side is left untouched;
            # scale in the PE (untested) side instead — one more PES,
            # riding the existing PEB. Strike = ATM + pe_scale_in_offset
            # (signed; 0 keeps the pre-existing default of landing exactly
            # on the original ATM strike, i.e. any currently-open PES
            # leg's strike, since PES never moves on its own).
            atm_strike = _strike_of(pes_legs[0])
            if atm_strike is not None:
                pe_scale_in_offset = float(p.get("pe_scale_in_offset_points") or 0)
                new_strike = _nearest_strike(strikes_avail, atm_strike + pe_scale_in_offset)
                row = _row(new_strike)
                if row is not None:
                    lot_size = get_lot_size(security_id=row["pe_security_id"]) or 75
                    quantity = lot_size * int(p["lots"])
                    new_leg = OrderLeg(
                        label=f"SCALE-IN PES SELL {int(new_strike)} PE ({expiry})",
                        security_id=str(row["pe_security_id"]),
                        trading_symbol=f"{underlying} {int(new_strike)} PE {expiry}",
                        exchange_segment=meta["option_segment"],
                        transaction_type="SELL",
                        quantity=quantity,
                        order_type=order_type,
                        product_type="MARGIN",
                        price=float(row["pe_ltp"]),
                        role="primary",
                        pair_id="PE",
                    )
                    rolls.append({
                        "close_security_ids": [],
                        "allow_empty_close": True,
                        "new_legs": [new_leg],
                        # Flag the (unchanged, still-open) CEB leg so this same
                        # boundary doesn't scale in the PE side again.
                        "leg_state_patch": {ceb_sid: {**ceb_state, "scaled_in": True}},
                    })

        if trigger_pe_side_touched:
            # Symmetric: PE side reached its wing -> scale in the CE
            # (untested) side — one more CES, riding the existing CEB.
            # Strike = ATM + ce_scale_in_offset (signed; 0 = original ATM).
            atm_strike = _strike_of(ces_legs[0])
            if atm_strike is not None:
                ce_scale_in_offset = float(p.get("ce_scale_in_offset_points") or 0)
                new_strike = _nearest_strike(strikes_avail, atm_strike + ce_scale_in_offset)
                row = _row(new_strike)
                if row is not None:
                    lot_size = get_lot_size(security_id=row["ce_security_id"]) or 75
                    quantity = lot_size * int(p["lots"])
                    new_leg = OrderLeg(
                        label=f"SCALE-IN CES SELL {int(new_strike)} CE ({expiry})",
                        security_id=str(row["ce_security_id"]),
                        trading_symbol=f"{underlying} {int(new_strike)} CE {expiry}",
                        exchange_segment=meta["option_segment"],
                        transaction_type="SELL",
                        quantity=quantity,
                        order_type=order_type,
                        product_type="MARGIN",
                        price=float(row["ce_ltp"]),
                        role="primary",
                        pair_id="CE",
                    )
                    rolls.append({
                        "close_security_ids": [],
                        "allow_empty_close": True,
                        "new_legs": [new_leg],
                        # Flag the (unchanged, still-open) PEB leg so this same
                        # boundary doesn't scale in the CE side again.
                        "leg_state_patch": {peb_sid: {**peb_state, "scaled_in": True}},
                    })

        if not rolls:
            return None
        return {"rolls": rolls}
