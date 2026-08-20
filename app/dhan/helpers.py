"""Composable Dhan helper functions, adapted from the dhanhq-skills reference
repo's scripts/dhan_helpers.py for multi-tenant use: every function here takes
an already-constructed per-user `dhan_client` instead of building one from
env vars.

The DhanHQ SDK wraps HTTP responses as:
    {"status": "success"|"failure", "remarks": ..., "data": ...}
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import pandas as pd
from dhanhq import dhanhq

logger = logging.getLogger("app.dhan")

_security_master_cache: pd.DataFrame | None = None

# Dhan allows only one option-chain request every 3 seconds, account-wide —
# far stricter than quote/ticker data, and undocumented in the SDK itself
# (see .reference/dhanhq-skills/skills/dhanhq/references/option-chain.md).
# The scheduler evaluates every active UserStrategy in one tick with no
# spacing between them; the moment a user has two or more active instances
# that each need a fresh chain (e.g. two strategies, or two instances of the
# same one), their option_chain calls land back-to-back well under 3s apart
# and Dhan throttles the second one — surfacing as an ambiguous
# "remarks: null" error that looks like nothing helpful. Serialize every
# option_chain call in this process through here, for every user and every
# strategy, so that never happens regardless of how many are active.
_option_chain_lock = threading.Lock()
_option_chain_last_call_at: float = 0.0
_OPTION_CHAIN_MIN_INTERVAL_SECONDS = 3.0

# Dhan's quote/ticker data ("market quote") family is rate-limited to one
# request/sec, account-wide — see fetch_quotes below. Unlike option_chain
# above, this had no throttle at all until 2026-08-20, and it showed: once
# the scheduler started evaluating a user's active strategies concurrently
# (a small thread pool, added 2026-08-19 for latency), two or more
# instances' quote_data/ticker_data calls routinely landed in the same
# second and Dhan rate-limited them — confirmed live via the VM's journalctl
# showing thousands of quote_data failures with the empty-remarks signature
# format_dhan_error() below already recognizes as "you're being
# rate-limited". Because every exit path (evaluate_leg_exits, evaluate_exit,
# evaluate_rolls, close_user_strategy_now) refuses to fabricate a price when
# the quote fetch fails, this silently stalled every SL/target/roll/close
# check for as long as the rate-limiting persisted — in the incident that
# surfaced this, that was continuously, all session. Serialize every
# quote_data/ticker_data call in this process the same way option_chain
# calls already are, so this can't recur regardless of how many strategies
# are active at once.
_quote_lock = threading.Lock()
_quote_last_call_at: float = 0.0
_QUOTE_MIN_INTERVAL_SECONDS = 1.0


def _call_with_retry(fn: Callable[[], Any], *, attempts: int = 2, base_delay: float = 1.0) -> Any:
    """Retry a read-only Dhan call once on a transient failure (timeout,
    connection reset), with a short backoff. Only ever wrap read-only calls
    with this — a retried write call (place_order) could double-fill if
    Dhan actually processed the first attempt but the response was lost in
    transit; every order placement in this app deliberately fails once
    rather than blindly resubmitting."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _throttle_option_chain() -> None:
    global _option_chain_last_call_at
    with _option_chain_lock:
        wait = _OPTION_CHAIN_MIN_INTERVAL_SECONDS - (time.monotonic() - _option_chain_last_call_at)
        if wait > 0:
            time.sleep(wait)
        _option_chain_last_call_at = time.monotonic()


def _throttle_quote() -> None:
    global _quote_last_call_at
    with _quote_lock:
        wait = _QUOTE_MIN_INTERVAL_SECONDS - (time.monotonic() - _quote_last_call_at)
        if wait > 0:
            time.sleep(wait)
        _quote_last_call_at = time.monotonic()

# Index underlyings quick-reference (from the dhanhq-skills SKILL.md).
# security_id is fixed by Dhan; exchange_segment is always the index segment
# (used for spot/option-chain lookups). option_segment is the *derivative*
# segment its actual option contracts trade on, which every strategy must
# tag each OrderLeg with — NSE indices' options trade on NSE_FNO, but
# SENSEX is a BSE index and its options trade on BSE_FNO. Never hardcode
# "NSE_FNO" on a leg; always pull it from here via the leg's underlying.
UNDERLYINGS: dict[str, dict[str, Any]] = {
    "NIFTY": {"security_id": 13, "exchange_segment": "IDX_I", "option_segment": "NSE_FNO", "label": "NIFTY 50"},
    "BANKNIFTY": {"security_id": 25, "exchange_segment": "IDX_I", "option_segment": "NSE_FNO", "label": "BANK NIFTY"},
    "FINNIFTY": {"security_id": 27, "exchange_segment": "IDX_I", "option_segment": "NSE_FNO", "label": "FINNIFTY"},
    "SENSEX": {"security_id": 51, "exchange_segment": "IDX_I", "option_segment": "BSE_FNO", "label": "SENSEX"},
}


def format_dhan_error(remarks: Any) -> str:
    """Turn the SDK's ``remarks`` field into a readable message.

    On a non-2xx HTTP response, dhanhq's http layer (`dhan_http.py`) builds
    `remarks` from the response body's `errorCode`/`errorType`/`errorMessage`
    keys. When Dhan's response doesn't use that shape — most commonly a 429
    rate-limit response — all three come back None and the raw dict is
    useless to show a user. Detect that specific case and say what's
    actually going on instead.
    """
    if isinstance(remarks, dict):
        code = remarks.get("error_code")
        etype = remarks.get("error_type")
        message = remarks.get("error_message")
        if not code and not etype and not message:
            return (
                "Dhan didn't return a specific error, which usually means "
                "you're being rate-limited (too many requests too quickly). "
                "Wait a few seconds and try again."
            )
        label = etype or "Error"
        suffix = f" ({code})" if code else ""
        return f"{label}: {message or 'no message'}{suffix}"
    if remarks:
        return str(remarks)
    return "Dhan SDK call failed"


def unwrap_sdk_data(response: dict[str, Any]) -> Any:
    """Return the ``data`` field from a successful SDK response."""
    if response.get("status") != "success":
        raise ValueError(format_dhan_error(response.get("remarks")))
    return response.get("data")


def _unwrap_nested(data: Any) -> Any:
    """Several endpoints double-wrap their payload: the SDK's own
    ``{"status", "data"}`` envelope contains *another*
    ``{"status": "success", "data": ...}`` envelope as its ``data``. Verified
    live against the real API (not just the dhanhq-skills reference docs,
    which are imprecise on this point) for option_chain, expiry_list,
    quote_data, and ticker_data — the whole option-chain and market-quote
    families share this quirk. Peel that second layer off when present."""
    if isinstance(data, dict) and "status" in data and "data" in data:
        return data["data"]
    return data


def fetch_quotes(dhan_client: "dhanhq", securities: dict[str, list[Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Fetch live quote snapshots, keyed by (exchange_segment, security_id_str).

    `securities` is Dhan's own request shape, e.g. {"NSE_FNO": [45106, 45093]}
    — group everything you need into as few segments as possible in one call;
    Dhan's quote API is rate-limited to 1 request/sec for the whole account,
    enforced here via `_throttle_quote` regardless of how many strategies
    are calling this concurrently (see the comment above `_quote_lock`).
    A failed call or a missing security in the response is simply absent
    from the returned dict — callers should treat a missing key as "no
    fresh quote available," not raise.
    """
    _throttle_quote()
    try:
        response = _call_with_retry(lambda: dhan_client.quote_data(securities))
    except Exception as exc:  # noqa: BLE001
        logger.warning("quote_data call raised for %s: %s: %s", securities, type(exc).__name__, exc)
        return {}
    if response.get("status") != "success":
        logger.warning("quote_data call failed for %s: %s", securities, response.get("remarks") or response)
        return {}
    data = _unwrap_nested(response.get("data"))
    if not isinstance(data, dict):
        logger.warning("quote_data returned an unexpected shape for %s: %r", securities, response.get("data"))
        return {}

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for segment, sid_map in data.items():
        if not isinstance(sid_map, dict):
            continue
        for sid, quote in sid_map.items():
            result[(segment, str(sid))] = quote

    # Individually-missing securities (present in the request, absent from
    # the response) are common and expected — not every symbol trades
    # every tick — so this stays at debug, not a warning like the failures
    # above.
    requested = {str(sid) for sids in securities.values() for sid in sids}
    returned = {sid for _, sid in result.keys()}
    missing = requested - returned
    if missing:
        logger.debug("quote_data response omitted %s (requested %s)", missing, requested)

    return result


def fetch_spot_price(dhan_client: "dhanhq", exchange_segment: str, security_id: int) -> float | None:
    """Fetch the current last-traded price for an index/underlying via
    ticker_data. Returns None (never raises) if the quote isn't available
    this call — callers should skip evaluation this pass, not guess."""
    _throttle_quote()
    try:
        response = _call_with_retry(lambda: dhan_client.ticker_data({exchange_segment: [security_id]}))
    except Exception:  # noqa: BLE001
        return None
    if response.get("status") != "success":
        return None
    data = _unwrap_nested(response.get("data"))
    if not isinstance(data, dict):
        return None
    quote = (data.get(exchange_segment) or {}).get(str(security_id))
    if not quote:
        return None
    return float(quote.get("last_price", 0)) or None


def fetch_expiry_list(dhan_client: "dhanhq", under_security_id: int, under_exchange_segment: str) -> list[str]:
    """Fetch the live list of expiry dates for a raw security_id/segment
    pair. Every strategy that needs an expiry should go through this (or
    `list_expiries` below) rather than calling `dhan_client.expiry_list`
    directly — it's the one place the double-wrap quirk is handled."""
    response = dhan_client.expiry_list(
        under_security_id=under_security_id,
        under_exchange_segment=under_exchange_segment,
    )
    data = _unwrap_nested(unwrap_sdk_data(response))
    return data or []


def list_expiries(dhan_client: "dhanhq", underlying: str) -> list[str]:
    """Fetch the live list of available expiry dates for a named underlying
    (see UNDERLYINGS), using the calling user's own Dhan connection. Used to
    populate the expiry dropdown on a strategy's configure page."""
    meta = UNDERLYINGS.get(underlying.upper())
    if meta is None:
        raise ValueError(f"Unknown underlying: {underlying!r}")
    return fetch_expiry_list(dhan_client, meta["security_id"], meta["exchange_segment"])


def get_security_master(mode: str = "compact") -> pd.DataFrame:
    """Fetch and cache the Dhan security master (shared across all users —
    this is public instrument reference data, not account-specific)."""
    global _security_master_cache
    if _security_master_cache is None:
        _security_master_cache = dhanhq.fetch_security_list(mode)
        if _security_master_cache is None:
            raise ValueError("Unable to fetch the Dhan security master")
    return _security_master_cache


def resolve_derivative(
    underlying: str,
    *,
    instrument_names: tuple[str, ...] = ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"),
    strike: float | None = None,
    option_type: str | None = None,
    expiry: str | None = None,
    exchange: str = "NSE",
) -> dict[str, Any] | None:
    """Resolve a derivative contract from the security master."""
    df = get_security_master()
    # SEM_CUSTOM_SYMBOL for a derivative is a full descriptive string (e.g.
    # "NIFTY 29 SEP 29150 CALL"), never the bare underlying name — match by
    # prefix, not equality, or this never matches anything.
    mask = (
        (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == exchange.upper())
        & (df["SEM_INSTRUMENT_NAME"].isin(instrument_names))
        & (df["SEM_CUSTOM_SYMBOL"].astype(str).str.upper().str.startswith(underlying.upper()))
    )
    if strike is not None:
        mask &= df["SEM_STRIKE_PRICE"].astype(float) == float(strike)
    if option_type is not None:
        mask &= df["SEM_OPTION_TYPE"].astype(str).str.upper() == option_type.upper()
    if expiry is not None:
        mask &= df["SEM_EXPIRY_DATE"].astype(str) == expiry

    matches = df[mask].sort_values(["SEM_EXPIRY_DATE", "SEM_TRADING_SYMBOL"])
    if matches.empty:
        return None

    row = matches.iloc[0]
    return {
        "security_id": str(row["SEM_SMST_SECURITY_ID"]),
        "trading_symbol": str(row["SEM_TRADING_SYMBOL"]),
        "lot_size": int(row["SEM_LOT_UNITS"]),
        "tick_size": float(row["SEM_TICK_SIZE"]),
        "expiry": str(row.get("SEM_EXPIRY_DATE", "")),
        "instrument_name": str(row["SEM_INSTRUMENT_NAME"]),
    }


def get_lot_size(*, security_id: str | None = None, underlying: str | None = None) -> int | None:
    """Return lot size from the security master. Never trust a hardcoded value —
    lot sizes change periodically; always resolve live before sizing an order."""
    df = get_security_master()

    if security_id is not None:
        match = df[df["SEM_SMST_SECURITY_ID"].astype(str) == str(security_id)]
        if not match.empty:
            return int(match.iloc[0]["SEM_LOT_UNITS"])

    if underlying is not None:
        match = df[
            (df["SEM_CUSTOM_SYMBOL"].astype(str).str.upper() == underlying.upper())
            & (df["SEM_INSTRUMENT_NAME"].isin(["OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"]))
        ]
        if not match.empty:
            return int(match.iloc[0]["SEM_LOT_UNITS"])

    return None


def normalize_option_chain(response: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """Normalize raw option-chain data into analysis-friendly rows.

    Normalized fields (``ce_ltp``, ``ce_oi``, ``ce_delta``, ...) are repo-defined
    conveniences — not raw Dhan field names.
    """
    data = _unwrap_nested(unwrap_sdk_data(response))
    spot = float(data["last_price"])
    option_chain = data.get("oc", {}) or {}

    rows: list[dict[str, Any]] = []
    for strike_key, strike_payload in sorted(option_chain.items(), key=lambda item: float(item[0])):
        row: dict[str, Any] = {"strike": float(strike_key)}
        for side in ("ce", "pe"):
            leg = strike_payload.get(side) or {}
            greeks = leg.get("greeks") or {}
            row[f"{side}_security_id"] = str(leg["security_id"]) if leg.get("security_id") is not None else None
            row[f"{side}_ltp"] = leg.get("last_price")
            row[f"{side}_oi"] = leg.get("oi")
            row[f"{side}_volume"] = leg.get("volume")
            row[f"{side}_iv"] = leg.get("implied_volatility")
            row[f"{side}_bid_price"] = leg.get("top_bid_price")
            row[f"{side}_ask_price"] = leg.get("top_ask_price")
            row[f"{side}_delta"] = greeks.get("delta")
            row[f"{side}_theta"] = greeks.get("theta")
        rows.append(row)

    return spot, rows


def fetch_chain_df(
    dhan_client: "dhanhq",
    under_security_id: int,
    expiry: str,
    under_exchange_segment: str = "IDX_I",
) -> tuple[pd.DataFrame, float]:
    """Fetch option-chain data for a given user's client and return a
    normalized DataFrame plus spot price."""
    _throttle_option_chain()
    response = dhan_client.option_chain(
        under_security_id=under_security_id,
        under_exchange_segment=under_exchange_segment,
        expiry=expiry,
    )
    spot, rows = normalize_option_chain(response)
    return pd.DataFrame(rows), spot


def find_atm_row(chain_df: pd.DataFrame, spot: float) -> pd.Series:
    """Return the nearest strike row to the provided spot value."""
    return chain_df.iloc[(chain_df["strike"] - spot).abs().argsort().iloc[0]]


def find_strike_by_nearest_premium(
    chain_df: pd.DataFrame,
    strikes: list[float],
    start_index: int,
    option_type: str,
    price_col: str,
    sid_col: str,
    target_premium: float,
    *,
    include_start: bool,
) -> tuple[float | None, "pd.Series | None"]:
    """Walk strikes outward from `start_index` on the OTM side for
    `option_type`, returning the (strike, row) whose premium is nearest
    `target_premium`. Shared by every strategy that needs "closest live
    premium" strike selection — used both to pick a strike by target
    premium and to pick a hedge leg the same way."""
    if option_type == "CE":
        indices = range(start_index if include_start else start_index + 1, len(strikes))
    else:
        indices = range(start_index if include_start else start_index - 1, -1, -1)

    best_row = None
    best_strike = None
    best_diff = None
    for idx in indices:
        strike = strikes[idx]
        matches = chain_df[chain_df["strike"] == strike]
        if matches.empty:
            continue
        row = matches.iloc[0]
        premium = row.get(price_col)
        if premium is None or not row.get(sid_col):
            continue
        diff = abs(float(premium) - target_premium)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_row = row
            best_strike = strike
    return best_strike, best_row


def check_margin(
    dhan_client: "dhanhq",
    *,
    security_id: str,
    exchange_segment: str,
    transaction_type: str,
    quantity: int,
    product_type: str,
    price: float,
    trigger_price: float = 0,
) -> dict[str, Any]:
    """Run a single-order margin check against the current user's account."""
    margin_response = dhan_client.margin_calculator(
        security_id=security_id,
        exchange_segment=exchange_segment,
        transaction_type=transaction_type,
        quantity=quantity,
        product_type=product_type,
        price=price,
        trigger_price=trigger_price,
    )
    funds_response = dhan_client.get_fund_limits()

    margin = unwrap_sdk_data(margin_response)
    funds = unwrap_sdk_data(funds_response)

    total_margin = margin.get("totalMargin", 0.0)
    available_balance = funds.get("availabelBalance", 0.0)

    return {
        "total_margin": total_margin,
        "available_balance": available_balance,
        "brokerage": margin.get("brokerage", 0.0),
        "sufficient": available_balance >= total_margin,
        "shortfall": max(0.0, total_margin - available_balance),
    }


def preview_order(
    security_id: str,
    exchange_segment: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    product_type: str,
    *,
    price: float = 0.0,
    trading_symbol: str | None = None,
) -> str:
    """Build a human-readable order preview for confirmation UI/logs."""
    notional = price * quantity if price else 0
    lines = [
        f"{transaction_type} {quantity} x {trading_symbol or security_id} "
        f"({exchange_segment}, {order_type}, {product_type})"
    ]
    if notional:
        lines.append(f"Notional: Rs. {notional:,.2f}")
    if notional > 50000:
        lines.append("Warning: notional exceeds Rs. 50,000")
    return " | ".join(lines)
