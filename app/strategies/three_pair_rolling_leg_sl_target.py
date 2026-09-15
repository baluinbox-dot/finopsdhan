"""Dynamic T-M-B 3-Pair Rolling Strategy — Individual Leg SL & Target.

A sibling of app.strategies.three_pair_rolling.ThreePairRollingStrategy —
same Top/Middle/Bottom rolling mechanics, but every CE and PE leg manages
its own stop-loss and target independently, instead of only ever exiting
as a whole pair together. Deliberately a separate file/class/code_ref, not
a modification of the original — existing instances of the original
strategy keep behaving exactly as they do today.

Per-leg rules (percent of that leg's own entry premium):
  - Stop-loss hit (premium rises to entry * (1 + leg_stop_loss_pct/100)):
    close only that leg.
  - Target hit (premium falls to entry * (1 - leg_target_pct/100)): close
    only that leg.
  - Either way, the sibling (the other side at the same strike) is left
    completely untouched — it keeps running on its own unmodified
    SL/target, entry-anchored, for as long as it's open. There is no
    "trail sibling to cost" reaction to a sibling's stop-loss (an earlier
    version of this strategy had one; removed on Balu's request 2026-09-15
    — a lone survivor should just run its own numbers, nothing more).
  - A pair/strike is *not* closed just because one side exits, nor — this
    is the part that differs from a plain "leave it alone" — is a
    surviving lone leg ever swept up by a later T/M/B roll once its
    strike leaves the window. It keeps running, completely decoupled from
    window membership, until it hits its *own* SL/target (or the whole
    run ends via the daily SL/target/end-time). Only a strike whose *both*
    legs are still open when the window rolls past it closes as a normal
    pair, exactly like the original strategy.

T/M/B rolling itself works exactly like the original strategy — reaching
the current Bottom shifts the window down (close the old Top pair, open a
new Bottom one gap below), reaching the current Top shifts it up — except
"close the old pair" only actually reverses real orders when *both* of
that strike's legs are still open (a clean, untouched pair). If one has
already exited on its own SL/target, the survivor is left running (see
above) and nothing is reversed for it; if both already exited, nothing is
reversed at all. The engine's `_apply_rolls` supports a roll that reverses
fewer legs than it names, or none, via an explicit, opt-in
`allow_empty_close` (added alongside this strategy — see
app/engine/runner.py; every other strategy's rolls are unaffected, since
none of them ever set it).

Window membership (which strikes still count as one of the 3 active T/M/B
slots, for the purpose of computing the next roll boundary only) is
tracked explicitly via each leg's `leg_state["in_window"]` rather than "is
it currently open" — a leg that closed via its own SL/target stays part
of the window (so its strike doesn't just vanish from the T/M/B
calculation) until a roll actually moves that slot away, at which point
every leg that was ever part of it (open or not, closed together with the
roll or left running solo per above) is patched to `in_window: False`.
Per-leg SL/target monitoring (`evaluate_leg_exits`) is deliberately *not*
gated by this flag — every open primary leg keeps being checked against
its own SL/target for as long as it's open, window membership or not;
`in_window` only ever affects the T/M/B boundary math in `evaluate_rolls`.

An optional hedge (one CE buy + one PE buy, picked by nearest live premium
to a target price, e.g. Rs 5 or Rs 10) protects the *combined* exposure of
all three pairs at once — identical to the original strategy's hedge:
sized at `lots * 3`, bought once at entry. From then on, exactly like the
original strategy, each side is independently re-hedged by
`evaluate_rolls` as spot approaches it: once spot comes within
`hedge_reentry_buffer` points of a hedge leg's own strike, that leg closes
and a fresh same-side hedge is bought, picked the same way from the
*current* spot. This is entirely independent of T/M/B rolling and of the
per-leg stop-loss/target above (all of which only ever touch
`role == "primary"` legs). `hedge_reentry_buffer` set to 0 (or hedging
disabled) keeps the hedge fixed all day, as before. Either way, any hedge
still open at day's end closes together with everything else via the
whole-position close path (daily stop-loss/target, end time, or a manual
Close Now).
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
from app.strategies.base import (
    OrderLeg,
    Strategy,
    StrategyContext,
    currently_open_legs,
    dedupe_legs_by_security_id,
    leg_option_type,
    leg_pnl,
    resolve_order_type,
)

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


def _leg_state(sid: str, leg_state: dict) -> dict:
    return {"status": "open", **(leg_state.get(sid) or {})}


def _in_window(sid: str, leg_state: dict) -> bool:
    """A leg counts as part of the current T/M/B window unless it's been
    explicitly rolled away (leg_state["in_window"] patched False by
    evaluate_rolls) — closing via its own SL/target does *not* remove it
    from the window; only an actual roll does."""
    return bool((leg_state.get(sid) or {}).get("in_window", True))


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


class ThreePairRollingLegSLTargetStrategy(Strategy):
    name = "3-Pair Rolling — Individual Leg SL & Target"
    description = (
        "The same Dynamic T-M-B 3-Pair Rolling window as the original 3-Pair Rolling "
        "strategy, but every CE and PE leg has its own stop-loss and target instead of "
        "exiting only as a whole pair. One leg hitting its stop or target closes only that "
        "leg — its sibling is untouched and keeps running on its own numbers, even after its "
        "strike later rolls out of the T/M/B window; a lone survivor only ever closes on its "
        "own SL/target (or the whole run ending). Only a still-fully-open pair rolls away "
        "together, exactly like the original strategy. A combined daily stop-loss/target and "
        "end time still close everything and stop the strategy for the day. Same optional "
        "combined hedge as the original strategy, including re-hedging itself as spot nears "
        "a hedge leg's own strike."
    )
    default_params = {
        "underlying": "NIFTY",
        "expiry": "",  # set at configure time from the live dropdown
        "lots": 1,
        "start_time": "09:20",
        "end_time": "14:45",
        "strike_gap": 50,
        "leg_stop_loss_pct": 25,  # 25 or 30, per leg's own entry premium
        "leg_target_pct": 80,  # 70 or 80, per leg's own entry premium
        "daily_stop_loss": 10000,
        "daily_target": 15000,
        "hedge_enabled": False,
        "hedge_premium_target": 5,  # buy the closest-premium CE/PE hedge to this price
        "hedge_reentry_buffer": 50,  # spot within this many points of a hedge strike -> close it and buy a fresh one; 0 disables re-entry
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    # --- entry: identical T/M/B window construction to the original strategy ---

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
            # per-pair lot count, identical to the original strategy. Picked
            # once, from the initial ATM, and never touched again today.
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

    # --- per-leg stop-loss / target ---

    def evaluate_leg_exits(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        """Every open primary leg is checked against its own fixed,
        entry-anchored SL/target — independent of every other leg, and
        independent of whether its strike is still one of the T/M/B
        window's 3 active slots (see module docstring: no sibling
        cost-trail, and no window-membership gating here)."""
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        leg_state = open_run_notes.get("leg_state") or {}
        sl_pct = float(p.get("leg_stop_loss_pct") or 0)
        target_pct = float(p.get("leg_target_pct") or 0)

        open_legs = [leg for leg in currently_open_legs(legs, leg_state) if leg.get("role") == "primary"]
        if not open_legs:
            return None

        securities_by_segment: dict[str, list[int]] = {}
        for leg in open_legs:
            securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
        quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

        close_ids: list[str] = []
        patch: dict[str, dict[str, Any]] = {}

        for leg in open_legs:
            sid = str(leg["security_id"])
            quote = quotes.get((leg["exchange_segment"], sid))
            if quote is None:
                continue  # no fresh price this pass — leave it, retry next poll
            premium = float(quote.get("last_price", 0))
            entry_price = float(leg["price"])
            sl_price = entry_price * (1 + sl_pct / 100)
            target_price = entry_price * (1 - target_pct / 100)

            if premium >= sl_price:
                close_ids.append(sid)
                patch[sid] = {**patch.get(sid, {}), "closed_reason": "leg_sl"}
            elif target_pct and premium <= target_price:
                close_ids.append(sid)
                patch[sid] = {**patch.get(sid, {}), "closed_reason": "leg_target"}

        if not close_ids:
            return None
        return {"close_security_ids": close_ids, "leg_state_patch": patch}

    # --- whole-run exit: end time + combined daily stop-loss/target ---

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
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

    # --- T/M/B rolling ---

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

        # Window membership uses in_window, not "currently open" — a strike
        # both of whose legs already exited via their own SL/target still
        # counts as an active slot until an actual roll moves it away (see
        # module docstring). Hedge legs don't apply here (this strategy has
        # none), but the role check is kept for parity with the sibling.
        # dedupe_legs_by_security_id first, same reason as everywhere else
        # in this file: legs is append-only, so a strike revisited after an
        # earlier roll away has two history entries sharing one security_id,
        # and leg_state (in_window included) only tracks the latest one.
        window_groups: dict[float, list[dict]] = {}
        for leg in dedupe_legs_by_security_id(legs):
            if leg.get("role") != "primary":
                continue
            sid = str(leg["security_id"])
            if not _in_window(sid, leg_state):
                continue
            strike = _strike_of(leg)
            if strike is None:
                continue
            window_groups.setdefault(strike, []).append(leg)

        open_strikes = sorted(window_groups.keys())
        window_ok = len(open_strikes) == 3
        if window_ok:
            b, m, t = open_strikes
        window: dict[float, list[dict]] = dict(window_groups)  # simulated state, updated as shifts are planned

        # Currently-open hedge legs (one per side), tracked separately from
        # the T/M/B window above — a hedge never joins it.
        hedge_legs: dict[str, dict] = {}
        for leg in currently_open_legs(legs, leg_state):
            if leg.get("role") == "hedge":
                option_type = leg_option_type(leg)
                if option_type:
                    hedge_legs[option_type] = leg

        spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])
        if spot is None:
            return None

        # A hedge leg is "due" for re-entry once spot has closed to within
        # `hedge_reentry_buffer` points of that leg's own strike — at that
        # point it's no longer the cheap far-OTM hedge it was bought as.
        hedge_buffer = float(p.get("hedge_reentry_buffer") or 0)
        hedge_due: dict[str, dict] = {}
        if p.get("hedge_enabled") and hedge_buffer > 0:
            for option_type, leg in hedge_legs.items():
                strike = _strike_of(leg)
                if strike is None:
                    continue
                if option_type == "PE" and strike <= spot and (spot - strike) <= hedge_buffer:
                    hedge_due[option_type] = leg
                elif option_type == "CE" and strike >= spot and (strike - spot) <= hedge_buffer:
                    hedge_due[option_type] = leg

        primary_shift_needed = window_ok and not (b < spot < t)
        if not primary_shift_needed and not hedge_due:
            return None  # nothing to do — skip the chain fetch entirely

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

        if primary_shift_needed:
            for _ in range(_MAX_SHIFTS_PER_PASS):
                if spot <= b:
                    target = _nearest_strike(strikes_avail, b - gap)
                    closing_strike = t
                elif spot >= t:
                    target = _nearest_strike(strikes_avail, t + gap)
                    closing_strike = b
                else:
                    break  # spot has settled back inside the (possibly already-shifted) window

                if target in window:
                    break  # shouldn't happen structurally — stop shifting rather than guess
                row = _row(target)
                if row is None:
                    break  # no valid contract at the target strike — stop here, retry next poll

                # Fresh lot size for the new pair — never assumed from whatever
                # quantity the closing strike happened to have (which may not
                # even exist if both its legs already exited independently).
                lot_size = get_lot_size(security_id=row["ce_security_id"]) or 75
                quantity = lot_size * int(p["lots"])

                closing_legs = window.pop(closing_strike)  # every leg ever part of this slot, open or already leg-exited
                still_open = [
                    leg for leg in closing_legs
                    if _leg_state(str(leg["security_id"]), leg_state)["status"] == "open"
                ]
                # Only reverse this slot's legs together when *both* are
                # still open — a clean, untouched pair rolling away exactly
                # like the original strategy. If one already exited on its
                # own SL/target, the survivor is left running (see module
                # docstring) and nothing is reversed for it here; if both
                # already exited, nothing is reversed at all either way.
                to_reverse = still_open if len(still_open) == len(closing_legs) else []

                new_legs = [
                    OrderLeg(
                        label=f"ROLL SELL {int(target)} CE ({expiry})", security_id=str(row["ce_security_id"]),
                        trading_symbol=f"{underlying} {int(target)} CE {expiry}", exchange_segment=meta["option_segment"],
                        transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                        price=float(row["ce_ltp"]), role="primary",
                    ),
                    OrderLeg(
                        label=f"ROLL SELL {int(target)} PE ({expiry})", security_id=str(row["pe_security_id"]),
                        trading_symbol=f"{underlying} {int(target)} PE {expiry}", exchange_segment=meta["option_segment"],
                        transaction_type="SELL", quantity=quantity, order_type=order_type, product_type="INTRADAY",
                        price=float(row["pe_ltp"]), role="primary",
                    ),
                ]
                rolls.append({
                    "close_security_ids": [str(leg["security_id"]) for leg in to_reverse],
                    "new_legs": new_legs,
                    # May legitimately have nothing to reverse at the old
                    # strike — either both legs already exited on their own
                    # SL/target, or exactly one did and its survivor is
                    # being deliberately left running rather than swept up.
                    "allow_empty_close": True,
                    "leg_state_patch": {str(leg["security_id"]): {"in_window": False} for leg in closing_legs},
                })
                window[target] = [asdict(leg) for leg in new_legs]

                # Re-derive B/M/T from the updated window for the next loop
                # check (handles a spot move spanning more than one gap).
                b, m, t = sorted(window.keys())

        if hedge_due:
            # Re-picked exactly like the entry-time hedge, just anchored to
            # the *current* ATM instead of the day's opening one — walking
            # outward from today's spot naturally lands past the old,
            # now-too-close strike.
            atm_strike = _nearest_strike(strikes_avail, spot)
            atm_index = strikes_avail.index(atm_strike)
            hedge_target = float(p.get("hedge_premium_target") or 0)
            for option_type, leg in hedge_due.items():
                price_col = "ce_ltp" if option_type == "CE" else "pe_ltp"
                sid_col = "ce_security_id" if option_type == "CE" else "pe_security_id"
                best_strike, best_row = find_strike_by_nearest_premium(
                    chain_df, strikes_avail, atm_index, option_type, price_col, sid_col, hedge_target, include_start=False,
                )
                if best_row is None:
                    continue  # no valid fresh-hedge candidate this pass — leave the old hedge in place, retry next poll
                if best_strike == _strike_of(leg):
                    continue  # nothing actually changed — don't close/reopen the same strike
                rolls.append({
                    "close_security_ids": [str(leg["security_id"])],
                    "new_legs": [OrderLeg(
                        label=f"HEDGE BUY {int(best_strike)} {option_type} ({expiry})",
                        security_id=str(best_row[sid_col]),
                        trading_symbol=f"{underlying} {int(best_strike)} {option_type} {expiry}",
                        exchange_segment=meta["option_segment"],
                        transaction_type="BUY",
                        quantity=leg["quantity"],
                        order_type=order_type,
                        product_type="INTRADAY",
                        price=float(best_row[price_col]),
                        role="hedge",
                    )],
                })

        if not rolls:
            return None
        return {"rolls": rolls}
