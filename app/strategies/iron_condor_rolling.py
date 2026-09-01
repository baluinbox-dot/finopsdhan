"""Iron Condor — Untested-Side Rolling.

A short Iron Condor (CEB/CES/PES/PEB — Call Buy/Sell + Put Sell/Buy) that
adjusts by rolling only the *untested* side closer to spot whenever the
market reaches the tested side's outer wing, instead of touching the
tested side itself or closing the whole condor. This is the standard
"roll the untested side" adjustment: moving the tested side away would
require paying a debit or admitting a directional loss, so it is left
alone; the untested side is dragged in to collect fresh credit and keep
the position balanced.

Unlike the other rolling strategies in this app, this one is **not**
intraday — it holds the position across multiple days (rolling as
needed, any day the market is open) and only force-closes on the expiry
day itself, at `end_time`. Legs are placed with `product_type="MARGIN"`
(not `"INTRADAY"`), because an INTRADAY product would get auto-squared-off
by the broker itself every day regardless of what this strategy wants.
`start_time`/`end_time` still bound the daily entry window (what time of
day a first entry may happen), but only the expiry day's `end_time` — not
every day's — triggers the whole-position close.

The roll gap is not a separate configured number — it's derived live from
the *triggering* (tested) side's own current wing width (CEB-CES, or
PEB-PES), so the untested side's new sell strike always lands exactly on
the tested side's current sell strike, and its new buy strike is that
same width beyond that.

Worked example (spot 24,000, sell_offset=250, buy_offset=350 -> initial
CE/PE width both 100):
  Entry: CEB 24350 buy, CES 24250 sell, PES 23750 sell, PEB 23650 buy.
  Spot rises to touch CEB (24350) -> CE side is *not* touched. Gap =
    CEB-CES = 100. The PE side is closed and re-opened: new PES = CEB-gap
    = 24250 (== the current CES strike), new PEB = new PES-gap = 24150.
    CE legs continue unchanged.
  Spot falls to touch PEB (23650) -> PE side is *not* touched. Gap =
    PES-PEB = 100. The CE side rolls symmetrically: new CES = PEB+gap =
    23750 (== the current PES strike), new CEB = new CES+gap = 23850.

Each side rolls at most once per distinct value of its own opposite-side
trigger strike — once PE has rolled in response to a given CEB, it won't
roll again just because spot keeps sitting at or above that same CEB;
tracked via `leg_state[<buy leg security_id>]["triggered_roll"]` on the
currently-open CEB/PEB leg. A later roll of the CE side itself creates a
brand new CEB leg with fresh (untriggered) state, so the chase mechanism
keeps working across repeated reversals. If price just keeps running past
the tested side without the position closing on it, only the whole-run
stop-loss/target (or the expiry-day close) eventually steps in — by
design, this strategy never moves the tested side to chase price.

Once a side has rolled, a *second, distinct* touch of the same still-open
boundary (spot must retreat back off it and return — sitting continuously
at/above it doesn't count as a second touch) scales the untested side's
own sell leg in further, up to `_MAX_ADDS_PER_BOUNDARY` times (currently
1): one more sell-only order for the same lot size, at the exact same
strike the untested side's sell leg is already at, blended into that
*same* leg record (combined quantity, weighted-average entry price) —
not a second independent leg on the same contract, and not a matching
new buy leg (the buy side's protection stays at its original size; each
add makes the untested side's own sold quantity larger relative to its
own wing's protection, deliberately). Tracked via `add_count`/`add_armed`
on the same buy leg that carries `triggered_roll`. See
`app.engine.runner._apply_increments` for why this has to blend into the
existing leg rather than append a new one (the same security_id sharing a
leg twice, independently, is exactly the class of bug that once made the
Dashboard mis-pair a hedge and a primary leg — see
`app.routers.dashboard._pair_orders`).

Stop-loss/target is a single combined check against the *whole* open
position (realized P&L so far this run + live mark-to-market on
everything still open), in one of two modes chosen per instance:
  - "fixed": stop_loss_value / target_value are rupee amounts.
  - "pct":   stop_loss_value / target_value are percentages of the total
    premium collected across every leg ever entered this run, across every
    day it's been open (initial entry plus every roll's new legs) — i.e.
    the position's own running credit, not a hardcoded budget.
Either firing closes every open leg and stops the strategy for good
(not just for the day, since the position is meant to run until expiry
regardless of the day it fires).
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_quotes, fetch_spot_price, get_lot_size
from app.strategies.base import OrderLeg, Strategy, StrategyContext, leg_pnl, resolve_order_type

IST = ZoneInfo("Asia/Kolkata")

# How many times the untested side's sell leg can be scaled in (see the
# module docstring's "scales the untested side's own sell leg in further"
# section) per boundary, on top of the size it already has. 1 means the
# sold quantity at that strike can at most double (original + one add).
_MAX_ADDS_PER_BOUNDARY = 1


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


class IronCondorRollingStrategy(Strategy):
    name = "Iron Condor — Untested-Side Rolling"
    description = (
        "A 4-leg short Iron Condor (buy/sell call + sell/buy put), held across multiple days (not "
        "intraday) and rolled as needed until expiry, that adjusts by rolling only the untested side "
        "closer to spot when the market reaches the tested side's outer wing — the tested side is "
        "never moved, only the opposite side is dragged in for fresh credit. A single combined "
        "stop-loss/target (fixed rupees or % of total premium collected) can close it early; "
        "otherwise it force-closes on the expiry day itself."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "expiry_type": "weekly",  # UI filter only ("weekly" | "monthly") — narrows the expiry dropdown
        "lots": 1,
        "start_time": "09:20",  # daily window during which a first entry may happen
        "end_time": "14:45",  # daily entry-window end, AND the force-close time on the expiry day itself
        "sell_offset_points": 250,  # CES/PES strike = spot +/- this
        "buy_offset_points": 350,  # CEB/PEB strike = spot +/- this (must be > sell_offset_points)
        "sl_target_mode": "fixed",  # "fixed" (rupees) | "pct" (of total premium collected this run so far)
        "stop_loss_value": 10000,
        "target_value": 15000,
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    # --- entry: 4 legs picked by fixed point offsets from spot ---

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

        sell_offset = float(p.get("sell_offset_points") or 0)
        buy_offset = float(p.get("buy_offset_points") or 0)
        if sell_offset <= 0 or buy_offset <= sell_offset:
            return None  # the buy wing must sit strictly further out than the sell strike

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        targets = {
            "CEB": _nearest_strike(strikes, spot + buy_offset),
            "CES": _nearest_strike(strikes, spot + sell_offset),
            "PES": _nearest_strike(strikes, spot - sell_offset),
            "PEB": _nearest_strike(strikes, spot - buy_offset),
        }
        # Never enter with a wing collapsed onto its own sell strike (offsets
        # too close together relative to the available strike spacing).
        if targets["CEB"] == targets["CES"] or targets["PES"] == targets["PEB"]:
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
    # days. It only force-closes once the *expiry date itself* has been
    # reached (never before), and even then only at end_time that day —
    # so a position from an earlier day is left alone by this check on
    # every day before expiry, no matter how late the clock gets.

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
        # open (initial entry + every roll's new legs) — the running credit
        # base for "pct" mode. Includes closed legs too; their entry
        # premium was collected regardless of whether that leg has since
        # been rolled away.
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

    # --- rolling: only the untested side moves ---

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

        def _open_side_legs(pair_id: str) -> list[dict]:
            return [
                leg for leg in legs
                if leg.get("pair_id") == pair_id and _leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
            ]

        ce_legs = _open_side_legs("CE")
        pe_legs = _open_side_legs("PE")
        if len(ce_legs) != 2 or len(pe_legs) != 2:
            return None  # not a clean 4-leg condor right now — don't guess, leave it alone

        ceb = next((leg for leg in ce_legs if leg["transaction_type"] == "BUY"), None)
        ces = next((leg for leg in ce_legs if leg["transaction_type"] == "SELL"), None)
        peb = next((leg for leg in pe_legs if leg["transaction_type"] == "BUY"), None)
        pes = next((leg for leg in pe_legs if leg["transaction_type"] == "SELL"), None)
        if ceb is None or ces is None or peb is None or pes is None:
            return None

        ceb_strike = _strike_of(ceb)
        ces_strike = _strike_of(ces)
        peb_strike = _strike_of(peb)
        pes_strike = _strike_of(pes)
        if ceb_strike is None or ces_strike is None or peb_strike is None or pes_strike is None:
            return None

        ceb_sid = str(ceb["security_id"])
        peb_sid = str(peb["security_id"])
        ceb_state = leg_state.get(ceb_sid) or {}
        peb_state = leg_state.get(peb_sid) or {}

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        ceb_touched = spot >= ceb_strike
        peb_touched = spot <= peb_strike

        # First touch of a boundary rolls the opposite side (existing
        # behaviour, unchanged). Once that's happened, a *later, distinct*
        # touch of the same still-open boundary instead scales the
        # opposite side's own sell leg in further (see module docstring) —
        # gated by `add_armed`, which only becomes true again once spot
        # has retreated off the boundary; sitting continuously at/above it
        # must not keep re-arming (or re-adding) on every poll.
        trigger_ce_side_touched = ceb_touched and not ceb_state.get("triggered_roll")
        trigger_pe_side_touched = peb_touched and not peb_state.get("triggered_roll")

        ce_add_count = int(ceb_state.get("add_count") or 0)
        pe_add_count = int(peb_state.get("add_count") or 0)
        ce_adds_available = ceb_state.get("triggered_roll") and ce_add_count < _MAX_ADDS_PER_BOUNDARY
        pe_adds_available = peb_state.get("triggered_roll") and pe_add_count < _MAX_ADDS_PER_BOUNDARY

        do_ce_add = ce_adds_available and ceb_touched and bool(ceb_state.get("add_armed"))
        do_pe_add = pe_adds_available and peb_touched and bool(peb_state.get("add_armed"))
        # Re-arm as soon as spot has retreated off a boundary that still
        # has an add available and isn't already armed — a state-only
        # update (see app.engine.runner._apply_rolls), no chain fetch
        # needed for this part.
        state_only_patch: dict[str, dict[str, Any]] = {}
        if ce_adds_available and not ceb_touched and not ceb_state.get("add_armed"):
            state_only_patch[ceb_sid] = {**ceb_state, "add_armed": True}
        if pe_adds_available and not peb_touched and not peb_state.get("add_armed"):
            state_only_patch[peb_sid] = {**peb_state, "add_armed": True}

        if not (trigger_ce_side_touched or trigger_pe_side_touched or do_ce_add or do_pe_add):
            if state_only_patch:
                return {"rolls": [{"close_security_ids": [], "new_legs": [], "leg_state_patch": state_only_patch}]}
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
        if state_only_patch:
            rolls.append({"close_security_ids": [], "new_legs": [], "leg_state_patch": state_only_patch})
        increments: list[dict[str, Any]] = []

        if do_ce_add:
            # PE side already rolled once in response to this CEB — a
            # second, distinct touch scales its own sell leg (currently at
            # pes_strike) in further, same lot size, blended into that
            # same leg rather than opened as a separate one.
            pes_row = _row(pes_strike)
            if pes_row is not None:
                lot_size = get_lot_size(security_id=pes_row["pe_security_id"]) or 75
                add_quantity = lot_size * int(p["lots"])
                increments.append({
                    "add_leg": OrderLeg(
                        label=f"ADD PES SELL {int(pes_strike)} PE ({expiry})", security_id=str(pes_row["pe_security_id"]),
                        trading_symbol=f"{underlying} {int(pes_strike)} PE {expiry}", exchange_segment=meta["option_segment"],
                        transaction_type="SELL", quantity=add_quantity, order_type=order_type, product_type="MARGIN",
                        price=float(pes_row["pe_ltp"]), role="primary", pair_id="PE",
                    ),
                    "leg_state_patch": {ceb_sid: {**ceb_state, "add_count": ce_add_count + 1, "add_armed": False}},
                })

        if do_pe_add:
            # Symmetric: CE side already rolled once in response to this
            # PEB — a second, distinct touch scales its own sell leg
            # (currently at ces_strike) in further.
            ces_row = _row(ces_strike)
            if ces_row is not None:
                lot_size = get_lot_size(security_id=ces_row["ce_security_id"]) or 75
                add_quantity = lot_size * int(p["lots"])
                increments.append({
                    "add_leg": OrderLeg(
                        label=f"ADD CES SELL {int(ces_strike)} CE ({expiry})", security_id=str(ces_row["ce_security_id"]),
                        trading_symbol=f"{underlying} {int(ces_strike)} CE {expiry}", exchange_segment=meta["option_segment"],
                        transaction_type="SELL", quantity=add_quantity, order_type=order_type, product_type="MARGIN",
                        price=float(ces_row["ce_ltp"]), role="primary", pair_id="CE",
                    ),
                    "leg_state_patch": {peb_sid: {**peb_state, "add_count": pe_add_count + 1, "add_armed": False}},
                })

        if trigger_ce_side_touched:
            # CE side reached its outer wing -> roll the PE (untested) side
            # in. The gap is the CE side's own current wing width (CEB-CES)
            # — not a separately configured number — so the new PES lands
            # exactly on the current CES strike, and new PEB is that same
            # width beyond it.
            ce_gap = ceb_strike - ces_strike
            if ce_gap > 0:
                new_pes_strike = _nearest_strike(strikes_avail, ceb_strike - ce_gap)
                new_peb_strike = _nearest_strike(strikes_avail, new_pes_strike - ce_gap)
            else:
                new_pes_strike = new_peb_strike = None  # malformed CE spread — don't guess
            if ce_gap > 0 and new_pes_strike != new_peb_strike:
                pes_row = _row(new_pes_strike)
                peb_row = _row(new_peb_strike)
                if pes_row is not None and peb_row is not None:
                    lot_size = get_lot_size(security_id=pes_row["pe_security_id"]) or 75
                    quantity = lot_size * int(p["lots"])
                    new_legs = [
                        OrderLeg(
                            label=f"ROLL PES SELL {int(new_pes_strike)} PE ({expiry})", security_id=str(pes_row["pe_security_id"]),
                            trading_symbol=f"{underlying} {int(new_pes_strike)} PE {expiry}", exchange_segment=meta["option_segment"],
                            transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="MARGIN",
                            price=float(pes_row["pe_ltp"]), role="primary", pair_id="PE",
                        ),
                        OrderLeg(
                            label=f"ROLL PEB BUY {int(new_peb_strike)} PE ({expiry})", security_id=str(peb_row["pe_security_id"]),
                            trading_symbol=f"{underlying} {int(new_peb_strike)} PE {expiry}", exchange_segment=meta["option_segment"],
                            transaction_type="BUY", quantity=quantity, order_type=order_type, product_type="MARGIN",
                            price=float(peb_row["pe_ltp"]), role="primary", pair_id="PE",
                        ),
                    ]
                    rolls.append({
                        "close_security_ids": [str(leg["security_id"]) for leg in pe_legs],
                        "new_legs": new_legs,
                        # Flag the (unchanged, still-open) CEB leg so this same
                        # boundary doesn't keep re-triggering a PE roll.
                        "leg_state_patch": {ceb_sid: {**ceb_state, "triggered_roll": True}},
                    })

        if trigger_pe_side_touched:
            # PE side reached its outer wing -> roll the CE (untested) side
            # in. The gap is the PE side's own current wing width (PES-PEB)
            # — so the new CES lands exactly on the current PES strike, and
            # new CEB is that same width beyond it.
            pe_gap = pes_strike - peb_strike
            if pe_gap > 0:
                new_ces_strike = _nearest_strike(strikes_avail, peb_strike + pe_gap)
                new_ceb_strike = _nearest_strike(strikes_avail, new_ces_strike + pe_gap)
            else:
                new_ces_strike = new_ceb_strike = None  # malformed PE spread — don't guess
            if pe_gap > 0 and new_ces_strike != new_ceb_strike:
                ces_row = _row(new_ces_strike)
                ceb_row = _row(new_ceb_strike)
                if ces_row is not None and ceb_row is not None:
                    lot_size = get_lot_size(security_id=ces_row["ce_security_id"]) or 75
                    quantity = lot_size * int(p["lots"])
                    new_legs = [
                        OrderLeg(
                            label=f"ROLL CES SELL {int(new_ces_strike)} CE ({expiry})", security_id=str(ces_row["ce_security_id"]),
                            trading_symbol=f"{underlying} {int(new_ces_strike)} CE {expiry}", exchange_segment=meta["option_segment"],
                            transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="MARGIN",
                            price=float(ces_row["ce_ltp"]), role="primary", pair_id="CE",
                        ),
                        OrderLeg(
                            label=f"ROLL CEB BUY {int(new_ceb_strike)} CE ({expiry})", security_id=str(ceb_row["ce_security_id"]),
                            trading_symbol=f"{underlying} {int(new_ceb_strike)} CE {expiry}", exchange_segment=meta["option_segment"],
                            transaction_type="BUY", quantity=quantity, order_type=order_type, product_type="MARGIN",
                            price=float(ceb_row["ce_ltp"]), role="primary", pair_id="CE",
                        ),
                    ]
                    rolls.append({
                        "close_security_ids": [str(leg["security_id"]) for leg in ce_legs],
                        "new_legs": new_legs,
                        # Flag the (unchanged, still-open) PEB leg so this same
                        # boundary doesn't keep re-triggering a CE roll.
                        "leg_state_patch": {peb_sid: {**peb_state, "triggered_roll": True}},
                    })

        result: dict[str, Any] = {}
        if rolls:
            result["rolls"] = rolls
        if increments:
            result["increments"] = increments
        return result or None
