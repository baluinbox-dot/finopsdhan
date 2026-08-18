"""Balu's first real strategy: sell a single OTM leg (CE or PE), picked
either by OTM level (nth strike from ATM) or by the strike whose live
premium is closest to a target price, optionally hedged by buying a
further-OTM same-side option also picked by nearest premium.

Stop-loss and target are each a *set* of independently-enabled conditions
(% move in combined premium, an absolute premium value, an absolute spot
level) — whichever enabled condition hits first closes the whole position,
hedge included. Runs only within a trading window that force-closes any
open position at its end.

One entry per day is enforced via `ctx.today_run_count` (populated by
`app/engine/runner.py`) rather than hardcoded here, so the gate is this
strategy's own choice, not an engine-wide rule.
"""

from __future__ import annotations

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
from app.strategies.base import OrderLeg, Strategy, StrategyContext

IST = ZoneInfo("Asia/Kolkata")

_DEFAULT_CONDITION = {"enabled": False, "value": 0}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = (value or "00:00").split(":")
    return dt_time(int(hour), int(minute))


def _condition(conditions: dict, key: str) -> dict:
    return {**_DEFAULT_CONDITION, **(conditions or {}).get(key, {})}


# Nearest-live-premium strike walking now lives in app.dhan.helpers as
# find_strike_by_nearest_premium — shared with atm_straddle_trigger_hedge.py.
_find_strike_by_premium = find_strike_by_nearest_premium


class SingleLegSellerWithHedge(Strategy):
    name = "Single-Leg Seller with Hedge"
    description = (
        "Sells one call or put — picked by OTM level or by closest live "
        "premium — with an optional same-side hedge picked by nearest "
        "premium. Stop-loss and target each support multiple independent "
        "conditions (% of premium, absolute premium, spot level) — "
        "whichever hits first closes the whole position, hedge included. "
        "Runs only within a trading window that force-closes any open "
        "position at its end. One entry per day."
    )
    default_params = {
        "underlying": "NIFTY",
        "option_type": "CE",  # CE | PE
        "strike_selection_mode": "otm_level",  # otm_level | premium_closest
        "otm_level": 1,  # 1 | 2 | 3 strikes away from ATM (strike_selection_mode == otm_level)
        "strike_premium_target": 0,  # target premium (strike_selection_mode == premium_closest)
        "expiry": "",  # set at configure time from the live dropdown
        "lots": 1,
        "stop_loss": {
            "premium_pct": {"enabled": True, "value": 30},
            "premium_abs": {"enabled": False, "value": 0},
            "spot_level": {"enabled": False, "value": 0},
        },
        "target": {
            "premium_pct": {"enabled": True, "value": 50},
            "premium_abs": {"enabled": False, "value": 0},
            "spot_level": {"enabled": False, "value": 0},
        },
        "hedge_enabled": False,
        "hedge_premium_target": 0,
        "window_start": "09:15",
        "window_end": "15:15",
    }

    def evaluate_entry(self, ctx: StrategyContext) -> list[OrderLeg] | None:
        p = {**self.default_params, **ctx.params}

        now_ist = _now_ist()
        window_start = _parse_hhmm(p["window_start"])
        window_end = _parse_hhmm(p["window_end"])
        if not (window_start <= now_ist.time() <= window_end):
            return None

        if ctx.today_run_count > 0:
            return None  # one entry per day already used

        underlying = str(p["underlying"]).upper()
        meta = UNDERLYINGS.get(underlying)
        if meta is None:
            return None

        option_type = str(p["option_type"]).upper()
        if option_type not in ("CE", "PE"):
            return None

        expiry = p.get("expiry")
        if not expiry:
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
        atm_strike = min(strikes, key=lambda x: abs(x - spot))
        atm_index = strikes.index(atm_strike)

        price_col = "ce_ltp" if option_type == "CE" else "pe_ltp"
        sid_col = "ce_security_id" if option_type == "CE" else "pe_security_id"

        mode = p.get("strike_selection_mode", "otm_level")
        if mode == "premium_closest":
            target_premium = float(p.get("strike_premium_target") or 0)
            target_strike, target_row = _find_strike_by_premium(
                chain_df, strikes, atm_index, option_type, price_col, sid_col, target_premium, include_start=True
            )
            if target_row is None:
                return None
            target_index = strikes.index(target_strike)
        else:
            otm_level = int(p["otm_level"])
            step = otm_level if option_type == "CE" else -otm_level
            target_index = atm_index + step
            if target_index < 0 or target_index >= len(strikes):
                return None  # not enough strikes on that side of the chain
            target_strike = strikes[target_index]
            target_matches = chain_df[chain_df["strike"] == target_strike]
            if target_matches.empty:
                return None
            target_row = target_matches.iloc[0]

        if not target_row.get(sid_col) or target_row.get(price_col) is None:
            return None

        # Resolve lot size from the *exact contract* being traded, not a
        # fuzzy underlying-name match — the security master's custom-symbol
        # field for options is a full descriptive string (e.g. "NIFTY 29 SEP
        # 29150 CALL"), never bare "NIFTY", so a name-based lookup silently
        # never matches and would fall through to a stale hardcoded default.
        # Lot sizes change periodically (e.g. NIFTY moved to 65); resolving
        # by security_id always gets the current, correct value.
        lot_size = get_lot_size(security_id=target_row[sid_col]) or 75
        quantity = lot_size * int(p["lots"])

        legs = [
            OrderLeg(
                label=f"SELL {int(target_strike)} {option_type} ({expiry})",
                security_id=str(target_row[sid_col]),
                trading_symbol=f"{underlying} {int(target_strike)} {option_type} {expiry}",
                exchange_segment="NSE_FNO",
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                product_type="INTRADAY",
                price=float(target_row[price_col]),
                role="primary",
            )
        ]

        if p.get("hedge_enabled"):
            hedge_target_premium = float(p.get("hedge_premium_target") or 0)
            best_strike, best_row = _find_strike_by_premium(
                chain_df, strikes, target_index, option_type, price_col, sid_col,
                hedge_target_premium, include_start=False,
            )

            if best_row is None:
                # Hedge was requested but no valid candidate strike was
                # found this pass — never go live naked when a hedge was
                # asked for; skip entry and retry next poll.
                return None

            legs.append(
                OrderLeg(
                    label=f"HEDGE BUY {int(best_strike)} {option_type} ({expiry})",
                    security_id=str(best_row[sid_col]),
                    trading_symbol=f"{underlying} {int(best_strike)} {option_type} {expiry}",
                    exchange_segment="NSE_FNO",
                    transaction_type="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    product_type="INTRADAY",
                    price=float(best_row[price_col]),
                    role="hedge",
                )
            )

        return legs

    def _stop_loss_hit(self, p: dict, *, current_premium, current_spot, entry_premium, option_type: str) -> bool:
        sl = p.get("stop_loss") or {}

        pct = _condition(sl, "premium_pct")
        if pct["enabled"] and current_premium is not None and entry_premium:
            change_pct = ((current_premium - entry_premium) / entry_premium) * 100
            if change_pct >= float(pct["value"]):
                return True

        abs_cfg = _condition(sl, "premium_abs")
        if abs_cfg["enabled"] and current_premium is not None:
            if current_premium >= float(abs_cfg["value"]):
                return True

        spot_cfg = _condition(sl, "spot_level")
        if spot_cfg["enabled"] and current_spot is not None:
            level = float(spot_cfg["value"])
            if option_type == "CE" and current_spot >= level:
                return True
            if option_type == "PE" and current_spot <= level:
                return True

        return False

    def _target_hit(self, p: dict, *, current_premium, current_spot, entry_premium, option_type: str) -> bool:
        tgt = p.get("target") or {}

        pct = _condition(tgt, "premium_pct")
        if pct["enabled"] and current_premium is not None and entry_premium:
            change_pct = ((current_premium - entry_premium) / entry_premium) * 100
            if change_pct <= -float(pct["value"]):
                return True

        abs_cfg = _condition(tgt, "premium_abs")
        if abs_cfg["enabled"] and current_premium is not None:
            if current_premium <= float(abs_cfg["value"]):
                return True

        spot_cfg = _condition(tgt, "spot_level")
        if spot_cfg["enabled"] and current_spot is not None:
            level = float(spot_cfg["value"])
            if option_type == "CE" and current_spot <= level:
                return True
            if option_type == "PE" and current_spot >= level:
                return True

        return False

    def evaluate_exit(self, ctx: StrategyContext, open_run_notes: dict[str, Any]) -> bool:
        p = {**self.default_params, **ctx.params}
        legs = open_run_notes.get("legs") or []
        entry_premium = open_run_notes.get("entry_premium")
        if not legs:
            return False

        # Time-window close always wins, regardless of P&L.
        window_end = _parse_hhmm(p["window_end"])
        if _now_ist().time() >= window_end:
            return True

        option_type = str(p["option_type"]).upper()
        sl = p.get("stop_loss") or {}
        tgt = p.get("target") or {}

        needs_premium = any(
            _condition(cfg, key)["enabled"] for cfg in (sl, tgt) for key in ("premium_pct", "premium_abs")
        )
        needs_spot = _condition(sl, "spot_level")["enabled"] or _condition(tgt, "spot_level")["enabled"]

        current_premium = None
        if needs_premium and entry_premium:
            securities_by_segment: dict[str, list[int]] = {}
            for leg in legs:
                securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
            quotes = fetch_quotes(ctx.dhan_client, securities_by_segment)

            current_premium = 0.0
            for leg in legs:
                quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
                if quote is None:
                    return False  # can't get a fresh quote this pass — don't guess
                last_price = float(quote.get("last_price", 0))
                sign = 1 if leg["transaction_type"] == "SELL" else -1
                current_premium += sign * last_price

        current_spot = None
        if needs_spot:
            meta = UNDERLYINGS.get(str(p["underlying"]).upper())
            if meta is not None:
                current_spot = fetch_spot_price(ctx.dhan_client, meta["exchange_segment"], meta["security_id"])

        kwargs = dict(
            current_premium=current_premium, current_spot=current_spot,
            entry_premium=entry_premium, option_type=option_type,
        )
        return self._stop_loss_hit(p, **kwargs) or self._target_hit(p, **kwargs)
