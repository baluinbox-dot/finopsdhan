"""Composable Dhan helper functions, adapted from the dhanhq-skills reference
repo's scripts/dhan_helpers.py for multi-tenant use: every function here takes
an already-constructed per-user `dhan_client` instead of building one from
env vars.

The DhanHQ SDK wraps HTTP responses as:
    {"status": "success"|"failure", "remarks": ..., "data": ...}
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from dhanhq import dhanhq

_security_master_cache: pd.DataFrame | None = None

# Index underlyings quick-reference (from the dhanhq-skills SKILL.md).
# security_id is fixed by Dhan; exchange_segment is always the index segment.
UNDERLYINGS: dict[str, dict[str, Any]] = {
    "NIFTY": {"security_id": 13, "exchange_segment": "IDX_I", "label": "NIFTY 50"},
    "BANKNIFTY": {"security_id": 25, "exchange_segment": "IDX_I", "label": "BANK NIFTY"},
    "FINNIFTY": {"security_id": 27, "exchange_segment": "IDX_I", "label": "FINNIFTY"},
    "SENSEX": {"security_id": 51, "exchange_segment": "IDX_I", "label": "SENSEX"},
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
    Dhan's quote API is rate-limited to 1 request/sec for the whole account.
    A failed call or a missing security in the response is simply absent
    from the returned dict — callers should treat a missing key as "no
    fresh quote available," not raise.
    """
    try:
        response = dhan_client.quote_data(securities)
    except Exception:  # noqa: BLE001
        return {}
    if response.get("status") != "success":
        return {}
    data = _unwrap_nested(response.get("data"))
    if not isinstance(data, dict):
        return {}

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for segment, sid_map in data.items():
        if not isinstance(sid_map, dict):
            continue
        for sid, quote in sid_map.items():
            result[(segment, str(sid))] = quote
    return result


def fetch_spot_price(dhan_client: "dhanhq", exchange_segment: str, security_id: int) -> float | None:
    """Fetch the current last-traded price for an index/underlying via
    ticker_data. Returns None (never raises) if the quote isn't available
    this call — callers should skip evaluation this pass, not guess."""
    try:
        response = dhan_client.ticker_data({exchange_segment: [security_id]})
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
