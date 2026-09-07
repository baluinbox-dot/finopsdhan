"""RSI Call Writing — Weekly Roll.

A single-leg short call, held across days (like Iron Condor Rolling /
Iron Fly Adjustments — not intraday), entered once per week off a daily
RSI(3) signal on the underlying's own daily closes, and rolled forward
to the next weekly expiry on expiry day itself rather than just closing.
Balu's spec, paraphrased from a slide he was evaluating (locked
parameters after 60+ backtests, per that slide):

  - Once per trading day, at a configured check time (`entry_check_time`,
    default 15:25 — close enough to the 15:30 close that the day's RSI
    reading is effectively final), if flat: compute RSI(`rsi_period`,
    default 3) on the underlying's own daily closes (Wilder's smoothing).
    If RSI was >= `rsi_cross_level` (70) yesterday and is < it today (a
    "crossed down through 70" event), sell one call at
    `spot * (1 + strike_offset_pct/100)` -> nearest available strike
    (default offset 1.0%, i.e. spot 25,000 -> ~25,250 strike).
  - Which expiry: always "the nearest weekly expiry that is NOT today" —
    on a normal day that's simply the current week's; on the expiry day
    itself (whether for a fresh entry or the roll below), it skips
    straight to *next* week rather than selling something expiring the
    same day.
  - Exit, checked every poll (not just once daily):
      - Stop-loss: current premium >= 150% of entry premium (default
        stop_loss_pct=50) -> close.
      - Profit-lock: once current premium has ever decayed to <= 65% of
        entry (default profit_lock_trigger_pct=35, i.e. 35% profit), the
        stop tightens to 85% of entry (default profit_lock_stop_pct=85)
        for the rest of the run — even if premium climbs back up before
        actually reaching that tightened stop. This is what
        `leg_state[sid]["profit_lock_armed"]` tracks: it only ever turns
        on, never off, once the trigger has fired.
  - Expiry-day roll: if the currently open leg's own expiry is today,
    close it and immediately open the equivalent fresh position (a new
    spot * (1 + offset%) strike recomputed at roll time, not the old
    strike carried forward) on the next resolved expiry.
  - After a stop-loss or profit-lock exit, the instance stays flat for
    the *rest of that week* — not just the rest of the day — until a
    fresh RSI signal on or after next Monday, or a manual "Enter Now"
    click. This is `ctx.week_run_count` (see app.strategies.base /
    app.engine.runner), the first strategy here to need it; RSI exit is
    deliberately not part of this at all — only the two premium-based
    rules above ever end a position early, RSI only ever triggers entry.

This is the first strategy in this app to use *historical* (not live)
Dhan data — `app.dhan.helpers.fetch_daily_closes` — to compute an
indicator, rather than only ever reading live spot/quotes/chain data.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, fetch_daily_closes, fetch_quotes, get_lot_size, list_expiries
from app.strategies.base import OrderLeg, Strategy, StrategyContext, currently_open_legs, resolve_order_type

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "15:25").split(":")
    return dt_time(int(hour), int(minute))


def _nearest_strike(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


def _leg_expiry(leg: dict) -> str | None:
    """Expiry string parsed from the trading_symbol this strategy builds
    itself (`"{underlying} {strike} CE {expiry}"`) — same convention every
    strategy in this app uses, just reading the 4th token instead of the
    usual 2nd (strike)."""
    tokens = (leg.get("trading_symbol") or "").split()
    return tokens[3] if len(tokens) >= 4 else None


def _rsi(closes: list[float], period: int) -> float | None:
    """Wilder's RSI over `closes` (oldest first). None if there isn't
    enough history (fewer than period+1 closes) to seed even the first
    average gain/loss."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _rsi_today_and_yesterday(closes: list[float], period: int) -> tuple[float | None, float | None]:
    """(rsi_yesterday, rsi_today) — computed independently from the same
    Wilder's-smoothing method over `closes` truncated at each point. Both
    recomputations start smoothing from the same seed window (index 0),
    so this reproduces the same historical values a genuine rolling
    series would have; simplest correct way to compare two consecutive
    days without maintaining a full rolling-RSI implementation, and cheap
    enough at this data size (~90 closes) to not matter."""
    today = _rsi(closes, period)
    yesterday = _rsi(closes[:-1], period) if len(closes) > 1 else None
    return yesterday, today


def _resolve_target_expiry(expiries: list[str], today: date) -> str | None:
    """The nearest expiry that is NOT today — used both for a fresh entry
    and for the expiry-day roll, so "never sell something expiring the
    same day" is enforced identically in both places. None if there's no
    upcoming expiry at all (shouldn't normally happen)."""
    candidates = sorted(e for e in expiries if date.fromisoformat(e) >= today)
    if not candidates:
        return None
    if date.fromisoformat(candidates[0]) == today:
        return candidates[1] if len(candidates) > 1 else None
    return candidates[0]


class RSICallWritingStrategy(Strategy):
    name = "RSI Call Writing — Weekly Roll"
    description = (
        "A single short call, held across days, entered once per week off a daily RSI(3) cross-down "
        "through 70 on the underlying's own daily closes (strike = spot + a configured % away, nearest "
        "available strike). Stop-loss at 150% of entry premium, tightening to 85% of entry once the "
        "position has ever reached 35% profit. Rolls to next week's equivalent strike on expiry day "
        "itself rather than just closing. After a stop fires, stays flat for the rest of that week."
    )
    default_params = {
        "underlying": "NIFTY",
        "lots": 1,
        "entry_check_time": "15:25",  # once-daily check, near/after the 15:30 close
        "rsi_period": 3,
        "rsi_cross_level": 70,
        "strike_offset_pct": 1.0,  # strike = spot * (1 + this/100)
        "stop_loss_pct": 50,  # premium rising to entry*(1+this/100) closes the position
        "profit_lock_trigger_pct": 35,  # once premium has decayed this % from entry...
        "profit_lock_stop_pct": 85,  # ...the stop tightens to this % of entry, permanently for this run
        "order_type": "LIMIT",  # "LIMIT" (safe default) or "MARKET" (no price protection)
    }

    # --- entry: once/week, off a daily RSI(3) cross-down through 70 ---

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}
        order_type = resolve_order_type(p)

        now_ist = _now_ist()
        check_time = _parse_hhmm(p["entry_check_time"])
        if now_ist.time() < check_time:
            return None

        if ctx.today_run_count > 0 or ctx.week_run_count > 0:
            return None  # already traded today, or stopped out earlier this week -- stay flat until next week

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None

        rsi_period = int(p.get("rsi_period") or 0)
        cross_level = float(p.get("rsi_cross_level") or 0)
        if rsi_period <= 0:
            return None

        closes = fetch_daily_closes(ctx.dhan_client, meta["security_id"], meta["exchange_segment"])
        rsi_yesterday, rsi_today = _rsi_today_and_yesterday(closes, rsi_period)
        if rsi_yesterday is None or rsi_today is None:
            return None  # not enough daily history yet -- don't guess
        if not (rsi_yesterday >= cross_level and rsi_today < cross_level):
            return None  # no cross-down today

        all_expiries = list_expiries(ctx.dhan_client, underlying)
        target_expiry = _resolve_target_expiry(all_expiries, now_ist.date())
        if not target_expiry:
            return None

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=target_expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        offset_pct = float(p.get("strike_offset_pct") or 0)
        strike = _nearest_strike(strikes, spot * (1 + offset_pct / 100))

        matches = chain_df[chain_df["strike"] == strike]
        if matches.empty:
            return None
        row = matches.iloc[0]
        if not row.get("ce_security_id") or row.get("ce_ltp") is None:
            return None

        lot_size = get_lot_size(security_id=row["ce_security_id"]) or 75
        quantity = lot_size * int(p["lots"])

        return [OrderLeg(
            label=f"SELL {int(strike)} CE ({target_expiry})",
            security_id=str(row["ce_security_id"]),
            trading_symbol=f"{underlying} {int(strike)} CE {target_expiry}",
            exchange_segment=meta["option_segment"],
            transaction_type="SELL",
            quantity=quantity,
            order_type=order_type,
            product_type="MARGIN",  # carried forward across days, NOT auto-squared-off intraday by the broker
            price=float(row["ce_ltp"]),
            role="primary",
        )]

    # --- exit: stop-loss + profit-lock, checked every poll ---

    def evaluate_leg_exits(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        leg_state = open_run_notes.get("leg_state") or {}
        open_legs = [leg for leg in currently_open_legs(legs, leg_state) if leg.get("role") == "primary"]
        if len(open_legs) != 1:
            return None  # not a clean single-leg position right now -- don't guess

        leg = open_legs[0]
        sid = str(leg["security_id"])
        entry_premium = float(leg["price"])
        if entry_premium <= 0:
            return None

        quotes = fetch_quotes(ctx.dhan_client, {leg["exchange_segment"]: [int(sid)]})
        quote = quotes.get((leg["exchange_segment"], sid))
        if quote is None:
            return None  # can't get a fresh quote this pass -- don't guess, retry next poll
        current_premium = float(quote.get("last_price", 0))

        state = leg_state.get(sid) or {}
        already_armed = bool(state.get("profit_lock_armed"))

        trigger_pct = float(p.get("profit_lock_trigger_pct") or 0)
        lock_stop_pct = float(p.get("profit_lock_stop_pct") or 0)
        stop_loss_pct = float(p.get("stop_loss_pct") or 0)

        # Once armed, stays armed for the rest of this run even if premium
        # climbs back up before actually reaching the tightened stop.
        newly_armed = (not already_armed) and trigger_pct > 0 and current_premium <= entry_premium * (1 - trigger_pct / 100)
        armed = already_armed or newly_armed

        if armed and lock_stop_pct > 0:
            should_close = current_premium >= entry_premium * (lock_stop_pct / 100)
        else:
            should_close = stop_loss_pct > 0 and current_premium >= entry_premium * (1 + stop_loss_pct / 100)

        if should_close:
            return {"close_security_ids": [sid]}
        if newly_armed:
            return {"close_security_ids": [], "leg_state_patch": {sid: {**state, "profit_lock_armed": True}}}
        return None

    # --- expiry-day roll: close + reopen fresh on next week's expiry ---

    def evaluate_rolls(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> dict[str, Any] | None:
        p = {**self.default_params, **ctx.params}
        order_type = resolve_order_type(p)

        now_ist = _now_ist()
        check_time = _parse_hhmm(p["entry_check_time"])
        if now_ist.time() < check_time:
            return None

        legs = open_run_notes.get("legs") or []
        if not legs:
            return None

        leg_state = open_run_notes.get("leg_state") or {}
        open_legs = [leg for leg in currently_open_legs(legs, leg_state) if leg.get("role") == "primary"]
        if len(open_legs) != 1:
            return None

        leg = open_legs[0]
        expiry_str = _leg_expiry(leg)
        if not expiry_str:
            return None
        try:
            expiry_date = date.fromisoformat(expiry_str)
        except ValueError:
            return None
        if now_ist.date() < expiry_date:
            return None  # not expiry day yet

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None

        all_expiries = list_expiries(ctx.dhan_client, underlying)
        target_expiry = _resolve_target_expiry(all_expiries, now_ist.date())
        if not target_expiry or target_expiry == expiry_str:
            return None  # nothing later available to roll into -- don't guess

        chain_df, spot = fetch_chain_df(
            ctx.dhan_client,
            under_security_id=meta["security_id"],
            expiry=target_expiry,
            under_exchange_segment=meta["exchange_segment"],
        )
        if chain_df.empty:
            return None

        strikes = sorted(chain_df["strike"].tolist())
        offset_pct = float(p.get("strike_offset_pct") or 0)
        strike = _nearest_strike(strikes, spot * (1 + offset_pct / 100))

        matches = chain_df[chain_df["strike"] == strike]
        if matches.empty:
            return None
        row = matches.iloc[0]
        if not row.get("ce_security_id") or row.get("ce_ltp") is None:
            return None

        quantity = leg["quantity"]
        new_leg = OrderLeg(
            label=f"ROLL SELL {int(strike)} CE ({target_expiry})",
            security_id=str(row["ce_security_id"]),
            trading_symbol=f"{underlying} {int(strike)} CE {target_expiry}",
            exchange_segment=meta["option_segment"],
            transaction_type="SELL",
            quantity=quantity,
            order_type=order_type,
            product_type="MARGIN",
            price=float(row["ce_ltp"]),
            role="primary",
        )
        return {"close_security_ids": [str(leg["security_id"])], "new_legs": [new_leg]}
