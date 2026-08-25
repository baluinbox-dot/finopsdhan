"""Strategy catalog: superadmin publishes strategies, users enable/configure
their own instance of a published strategy against their connected Dhan
account."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.dhan.client import DhanNotConnectedError, get_user_dhan_client
from app.dhan.helpers import UNDERLYINGS, fetch_chain_df, get_lot_size, list_expiries
from app.deps import CurrentUser, DbSession, SuperadminUser
from app.engine.runner import close_user_strategy_now, enter_user_strategy_now, find_open_run
from app.models import Strategy, StrategyMode, UserStrategy
from app.strategies.registry import RICH_CONFIG_STRATEGIES, STRATEGY_REGISTRY, get_strategy_class
from app.templating import flash, render, url

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _classify_expiries(expiries: list[str]) -> dict[str, str]:
    """Labels each 'YYYY-MM-DD' expiry as "monthly" (the last expiry of its
    calendar month in the given list) or "weekly" (every other one) — Dhan's
    expiry_list has no such flag itself, so it's derived here purely from
    the dates returned. Unparseable entries are left out of the map (and so
    are never treated as "monthly")."""
    by_month: dict[tuple[int, int], list[str]] = defaultdict(list)
    for expiry in expiries:
        try:
            parsed = date.fromisoformat(expiry)
        except ValueError:
            continue
        by_month[(parsed.year, parsed.month)].append(expiry)
    monthly = {max(dates) for dates in by_month.values()}
    return {expiry: ("monthly" if expiry in monthly else "weekly") for expiry in expiries}


def _resolve_requested_mode(request: Request, mode: str, live_confirmed: bool) -> StrategyMode:
    """Turn the submitted Mode + Live-confirmation checkbox into an actual
    StrategyMode. LIVE only when BOTH the user explicitly checked the
    confirmation checkbox on this exact submission AND the server's own
    ALLOW_LIVE_TRADING master switch is on — app.engine.runner mirrors
    this same double-gate before ever placing a real order (see its
    module docstring: `is_live = user_strategy.mode == LIVE and
    settings.allow_live_trading`), so an instance saved as LIVE here can
    still never actually trade live if the server switch is off; this
    just keeps the UI from claiming success it can't back up. Falls back
    to PAPER with a clear flash message for any of: Mode wasn't actually
    Live, the checkbox wasn't checked, or the server-side switch is off."""
    if mode != "live":
        return StrategyMode.PAPER
    if not live_confirmed:
        flash(
            request,
            "Live mode requires checking the confirmation box below — the strategy has "
            "been turned on in paper mode instead.",
            "warning",
        )
        return StrategyMode.PAPER
    if not get_settings().allow_live_trading:
        flash(
            request,
            "Live trading isn't enabled on this server yet — the strategy has been "
            "turned on in paper mode instead.",
            "warning",
        )
        return StrategyMode.PAPER
    flash(
        request,
        "LIVE mode confirmed — this instance will place real orders with real money on "
        "your connected Dhan account.",
        "warning",
    )
    return StrategyMode.LIVE


@router.get("")
def list_strategies(request: Request, db: DbSession, current_user: CurrentUser):
    published = db.scalars(select(Strategy).where(Strategy.is_published == True)).all()  # noqa: E712

    # A user may run several instances of the *same* strategy concurrently
    # (e.g. a CE seller and a PE seller both active at once) — group by
    # strategy_id rather than assuming exactly one.
    my_instances: dict[uuid.UUID, list[UserStrategy]] = {}
    for us in db.scalars(select(UserStrategy).where(UserStrategy.user_id == current_user.id)):
        my_instances.setdefault(us.strategy_id, []).append(us)

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    return render(
        request,
        "strategies/list.html",
        {
            "current_user": current_user,
            "strategies": published,
            "my_instances": my_instances,
            "has_dhan": has_dhan,
            "rich_config_strategies": RICH_CONFIG_STRATEGIES,
        },
    )


@router.post("/{strategy_id}/enable")
def enable_strategy(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    lots: int = Form(1),
    strike_offset_points: int = Form(200),
    stop_loss_pct: int = Form(30),
    target_pct: int = Form(50),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "lots": lots,
        "strike_offset_points": strike_offset_points,
        "stop_loss_pct": stop_loss_pct,
        "target_pct": target_pct,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    existing = db.scalar(
        select(UserStrategy).where(
            UserStrategy.user_id == current_user.id, UserStrategy.strategy_id == strategy_id
        )
    )
    if existing:
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{strategy.name} enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.get("/{strategy_id}/configure")
def configure_strategy_form(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = "",
    underlying: str = "",
    option_type: str = "",
    expiry: str = "",
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    # Editing an existing instance (came from "Reconfigure") vs a blank form
    # for a brand new one ("Add New Instance") — both live at this same URL,
    # distinguished by whether user_strategy_id was passed.
    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    existing_params = existing.params if existing else {}
    underlying = (underlying or existing_params.get("underlying") or "NIFTY").upper()
    if underlying not in UNDERLYINGS:
        underlying = "NIFTY"
    option_type = (option_type or existing_params.get("option_type") or "CE").upper()
    if option_type not in ("CE", "PE"):
        option_type = "CE"
    if not expiry:
        expiry = existing_params.get("expiry") or ""

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    expiries: list[str] = []
    expiry_error: str | None = None
    otm_preview: list[dict] = []
    selected_expiry = expiry

    if has_dhan:
        try:
            user_dhan = get_user_dhan_client(db, current_user)
            expiries = list_expiries(user_dhan.client, underlying)
        except DhanNotConnectedError as exc:
            expiry_error = str(exc)
        except Exception as exc:  # noqa: BLE001 — surface any SDK/network error, don't crash the page
            expiry_error = f"Could not fetch expiries from Dhan: {exc}"

        if not selected_expiry and expiries:
            selected_expiry = expiries[0]  # default to nearest on first load

        if selected_expiry and selected_expiry in expiries:
            try:
                meta = UNDERLYINGS[underlying]
                chain_df, spot = fetch_chain_df(
                    user_dhan.client,
                    under_security_id=meta["security_id"],
                    expiry=selected_expiry,
                    under_exchange_segment=meta["exchange_segment"],
                )
                if not chain_df.empty:
                    strikes = sorted(chain_df["strike"].tolist())
                    atm_strike = min(strikes, key=lambda x: abs(x - spot))
                    atm_index = strikes.index(atm_strike)
                    price_col = "ce_ltp" if option_type == "CE" else "pe_ltp"
                    sid_col = "ce_security_id" if option_type == "CE" else "pe_security_id"
                    # Negative = ITM, 0 = ATM, positive = OTM — matches the
                    # strategy's own step = level if CE else -level math
                    # exactly, so no separate ITM/OTM code path is needed.
                    for level in range(-3, 11):
                        idx = atm_index + (level if option_type == "CE" else -level)
                        if 0 <= idx < len(strikes):
                            row = chain_df[chain_df["strike"] == strikes[idx]].iloc[0]
                            lot_size = get_lot_size(security_id=row.get(sid_col)) if row.get(sid_col) else None
                            strike_label = "ATM" if level == 0 else (f"ITM{-level}" if level < 0 else f"OTM{level}")
                            otm_preview.append(
                                {
                                    "level": level,
                                    "strike_label": strike_label,
                                    "strike": strikes[idx],
                                    "premium": row.get(price_col),
                                    "lot_size": lot_size,
                                }
                            )
            except Exception as exc:  # noqa: BLE001 — preview is a nice-to-have, never block the form
                expiry_error = expiry_error or f"Could not fetch live strikes: {exc}"

    # Merge order matters: the *class's* default_params is the authoritative
    # structural shape (always has every nested key) — the DB row's own
    # default_params (admin-tunable, may be partial or empty) layers on top,
    # then this user's saved params (which may predate a shape change, e.g.
    # an older flat stop-loss config) layers on last.
    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
        class_defaults = strategy_cls.default_params
    except ValueError:
        class_defaults = {}
    params = {**class_defaults, **strategy.default_params, **existing_params}
    # Reflect the dropdowns' current selection, not necessarily the saved value.
    params["underlying"] = underlying
    params["option_type"] = option_type

    return render(
        request,
        "strategies/configure_single_leg_hedge.html",
        {
            "current_user": current_user,
            "user_strategy_id": user_strategy_id,
            "strategy": strategy,
            "has_dhan": has_dhan,
            "underlyings": UNDERLYINGS,
            "selected_underlying": underlying,
            "selected_option_type": option_type,
            "selected_expiry": selected_expiry,
            "expiries": expiries,
            "expiry_error": expiry_error,
            "otm_preview": otm_preview,
            "params": params,
            "existing": existing,
        },
    )


@router.post("/{strategy_id}/configure")
def configure_strategy_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = Form(""),
    label: str = Form(""),
    underlying: str = Form(...),
    option_type: str = Form(...),
    strike_selection_mode: str = Form("otm_level"),
    otm_level: int = Form(1),
    strike_premium_target: float = Form(0),
    expiry: str = Form(...),
    lots: int = Form(1),
    sl_premium_pct_enabled: bool = Form(False),
    sl_premium_pct_value: float = Form(30),
    sl_premium_abs_enabled: bool = Form(False),
    sl_premium_abs_value: float = Form(0),
    sl_spot_enabled: bool = Form(False),
    sl_spot_value: float = Form(0),
    target_premium_pct_enabled: bool = Form(False),
    target_premium_pct_value: float = Form(50),
    target_premium_abs_enabled: bool = Form(False),
    target_premium_abs_value: float = Form(0),
    target_spot_enabled: bool = Form(False),
    target_spot_value: float = Form(0),
    hedge_enabled: bool = Form(False),
    hedge_premium_target: float = Form(0),
    window_start: str = Form("09:15"),
    window_end: str = Form("15:15"),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if underlying.upper() not in UNDERLYINGS:
        flash(request, "Unknown underlying.", "error")
        return RedirectResponse(url(f"/strategies/{strategy_id}/configure"), status_code=303)

    if not expiry:
        flash(request, "Pick an expiry before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure?underlying={underlying}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "underlying": underlying.upper(),
        "option_type": option_type.upper(),
        "strike_selection_mode": strike_selection_mode if strike_selection_mode == "premium_closest" else "otm_level",
        "otm_level": otm_level,
        "strike_premium_target": strike_premium_target,
        "expiry": expiry,
        "lots": lots,
        "stop_loss": {
            "premium_pct": {"enabled": sl_premium_pct_enabled, "value": sl_premium_pct_value},
            "premium_abs": {"enabled": sl_premium_abs_enabled, "value": sl_premium_abs_value},
            "spot_level": {"enabled": sl_spot_enabled, "value": sl_spot_value},
        },
        "target": {
            "premium_pct": {"enabled": target_premium_pct_enabled, "value": target_premium_pct_value},
            "premium_abs": {"enabled": target_premium_abs_enabled, "value": target_premium_abs_value},
            "spot_level": {"enabled": target_spot_enabled, "value": target_spot_value},
        },
        "hedge_enabled": hedge_enabled,
        "hedge_premium_target": hedge_premium_target,
        "window_start": window_start,
        "window_end": window_end,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    # Editing an existing instance (user_strategy_id was passed, e.g. from
    # "Reconfigure") updates it in place; otherwise this always creates a
    # brand new instance — a user can run several instances of the same
    # strategy concurrently (e.g. a CE seller and a PE seller both active).
    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    strike_desc = (
        f"OTM{params['otm_level']}" if params["strike_selection_mode"] == "otm_level" else f"~₹{params['strike_premium_target']:.0f}"
    )
    final_label = label.strip() or f"{params['option_type']} {strike_desc} {params['underlying']}"

    if existing:
        existing.label = final_label
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                label=final_label,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{final_label} configured and enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.post("/instance/{user_strategy_id}/disable")
def disable_instance(request: Request, db: DbSession, current_user: CurrentUser, user_strategy_id: uuid.UUID):
    """Pause one specific instance — does not touch any other instance of
    the same (or a different) strategy this user has running."""
    us = db.get(UserStrategy, user_strategy_id)
    if us is None or us.user_id != current_user.id:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)
    us.is_active = False
    db.commit()
    flash(request, f"{us.label or us.strategy.name} disabled.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.post("/instance/{user_strategy_id}/resume")
def resume_instance(request: Request, db: DbSession, current_user: CurrentUser, user_strategy_id: uuid.UUID):
    """Reactivate a previously disabled instance with its existing params."""
    us = db.get(UserStrategy, user_strategy_id)
    if us is None or us.user_id != current_user.id:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)
    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before resuming a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)
    us.is_active = True
    db.commit()
    flash(request, f"{us.label or us.strategy.name} resumed.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.post("/instance/{user_strategy_id}/delete")
def delete_instance(request: Request, db: DbSession, current_user: CurrentUser, user_strategy_id: uuid.UUID):
    """Permanently remove one instance and its run/order history. Blocked
    while a position is open — close it first so it doesn't vanish
    unresolved."""
    us = db.get(UserStrategy, user_strategy_id)
    if us is None or us.user_id != current_user.id:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if find_open_run(us) is not None:
        flash(request, "Close the open position first (Close Now on the Dashboard) before deleting this instance.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    label = us.label or us.strategy.name
    db.delete(us)
    db.commit()
    flash(request, f"{label} deleted.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.post("/{user_strategy_id}/close-now")
def close_now(request: Request, db: DbSession, current_user: CurrentUser, user_strategy_id: uuid.UUID):
    user_strategy = db.get(UserStrategy, user_strategy_id)
    if user_strategy is None or user_strategy.user_id != current_user.id:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url("/dashboard"), status_code=303)

    try:
        closed = close_user_strategy_now(db, user_strategy)
    except DhanNotConnectedError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url("/dashboard"), status_code=303)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Could not close position: {exc}", "error")
        return RedirectResponse(url("/dashboard"), status_code=303)

    if closed:
        flash(request, "Position closed — all legs, including any hedge, have been reversed.", "success")
    else:
        flash(request, "No open position to close.", "info")
    return RedirectResponse(url("/dashboard"), status_code=303)


@router.post("/{user_strategy_id}/enter-now")
def enter_now(request: Request, db: DbSession, current_user: CurrentUser, user_strategy_id: uuid.UUID):
    """Manually trigger an entry attempt right now — the one deliberate
    way to trade again today after a close (automatic or manual) has
    otherwise stopped the scheduler from re-entering this instance on its
    own for the rest of the day. Still requires the strategy's real entry
    conditions to actually be met; this doesn't force a trade blindly."""
    user_strategy = db.get(UserStrategy, user_strategy_id)
    if user_strategy is None or user_strategy.user_id != current_user.id:
        flash(request, "Strategy instance not found.", "error")
        return RedirectResponse(url("/dashboard"), status_code=303)

    try:
        entered = enter_user_strategy_now(db, user_strategy)
    except DhanNotConnectedError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url("/dashboard"), status_code=303)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url("/dashboard"), status_code=303)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Could not enter a position: {exc}", "error")
        return RedirectResponse(url("/dashboard"), status_code=303)

    if entered:
        flash(request, "Entry conditions were met — position opened.", "success")
    else:
        flash(request, "Entry conditions aren't met right now — nothing was entered. Try again later.", "info")
    return RedirectResponse(url("/dashboard"), status_code=303)


@router.post("/{strategy_id}/disable")
def disable_strategy(request: Request, db: DbSession, current_user: CurrentUser, strategy_id: uuid.UUID):
    existing = db.scalar(
        select(UserStrategy).where(
            UserStrategy.user_id == current_user.id, UserStrategy.strategy_id == strategy_id
        )
    )
    if existing:
        existing.is_active = False
        db.commit()
        flash(request, "Strategy disabled.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.get("/{strategy_id}/configure-rolling")
def configure_rolling_form(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = "",
    underlying: str = "",
    expiry: str = "",
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    existing_params = existing.params if existing else {}
    underlying = (underlying or existing_params.get("underlying") or "NIFTY").upper()
    if underlying not in UNDERLYINGS:
        underlying = "NIFTY"
    if not expiry:
        expiry = existing_params.get("expiry") or ""

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
        class_defaults = strategy_cls.default_params
    except ValueError:
        class_defaults = {}
    params = {**class_defaults, **strategy.default_params, **existing_params}
    params["underlying"] = underlying
    # NIFTY/FINNIFTY trade in 50-point strikes, BANKNIFTY/SENSEX in 100 —
    # defaulting everyone to 50 meant a BANKNIFTY/SENSEX preview silently
    # searched for a strike that doesn't exist (e.g. ATM+50 with only
    # 100-point strikes available lands exactly between two real strikes),
    # producing a nonsensical T==M preview. A saved strike_gap is only
    # respected as-is when it was saved *for this same underlying* — if the
    # user switches the Underlying dropdown to a different instrument (new
    # instance, or reconfiguring an existing one), the gap it inherited from
    # whatever underlying it was saved with is meaningless here and must be
    # re-defaulted for the underlying actually being previewed.
    if "strike_gap" not in existing_params or existing_params.get("underlying") != underlying:
        params["strike_gap"] = 100 if underlying in ("BANKNIFTY", "SENSEX") else 50

    expiries: list[str] = []
    expiry_error: str | None = None
    atm_preview: dict | None = None
    selected_expiry = expiry

    if has_dhan:
        try:
            user_dhan = get_user_dhan_client(db, current_user)
            expiries = list_expiries(user_dhan.client, underlying)
        except DhanNotConnectedError as exc:
            expiry_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            expiry_error = f"Could not fetch expiries from Dhan: {exc}"

        if not selected_expiry and expiries:
            selected_expiry = expiries[0]

        if selected_expiry and selected_expiry in expiries:
            try:
                meta = UNDERLYINGS[underlying]
                chain_df, spot = fetch_chain_df(
                    user_dhan.client,
                    under_security_id=meta["security_id"],
                    expiry=selected_expiry,
                    under_exchange_segment=meta["exchange_segment"],
                )
                if not chain_df.empty:
                    strikes = sorted(chain_df["strike"].tolist())
                    atm_strike = min(strikes, key=lambda x: abs(x - spot))
                    gap = float(params["strike_gap"] or 50)  # same gap the form will actually use, not a hardcoded guess
                    top = min(strikes, key=lambda x: abs(x - (atm_strike + gap)))
                    bottom = min(strikes, key=lambda x: abs(x - (atm_strike - gap)))
                    atm_preview = {"spot": spot, "top": top, "middle": atm_strike, "bottom": bottom, "gap": gap}
            except Exception as exc:  # noqa: BLE001 — preview is a nice-to-have, never block the form
                expiry_error = expiry_error or f"Could not fetch live strikes: {exc}"

    return render(
        request,
        "strategies/configure_rolling.html",
        {
            "current_user": current_user,
            "user_strategy_id": user_strategy_id,
            "strategy": strategy,
            "has_dhan": has_dhan,
            "underlyings": UNDERLYINGS,
            "selected_underlying": underlying,
            "selected_expiry": selected_expiry,
            "expiries": expiries,
            "expiry_error": expiry_error,
            "atm_preview": atm_preview,
            "params": params,
            "existing": existing,
        },
    )


@router.post("/{strategy_id}/configure-rolling")
def configure_rolling_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = Form(""),
    label: str = Form(""),
    underlying: str = Form(...),
    expiry: str = Form(...),
    lots: int = Form(1),
    start_time: str = Form("09:20"),
    end_time: str = Form("14:45"),
    strike_gap: float = Form(50),
    daily_stop_loss: float = Form(10000),
    daily_target: float = Form(15000),
    hedge_enabled: bool = Form(False),
    hedge_premium_target: float = Form(5),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if underlying.upper() not in UNDERLYINGS:
        flash(request, "Unknown underlying.", "error")
        return RedirectResponse(url(f"/strategies/{strategy_id}/configure-rolling"), status_code=303)

    if not expiry:
        flash(request, "Pick an expiry before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling?underlying={underlying}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    if strike_gap <= 0:
        flash(request, "Strike gap must be greater than zero.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "underlying": underlying.upper(),
        "expiry": expiry,
        "lots": lots,
        "start_time": start_time,
        "end_time": end_time,
        "strike_gap": strike_gap,
        "daily_stop_loss": daily_stop_loss,
        "daily_target": daily_target,
        "hedge_enabled": hedge_enabled,
        "hedge_premium_target": hedge_premium_target,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    final_label = label.strip() or f"3-Pair Rolling {params['underlying']}"

    if existing:
        existing.label = final_label
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                label=final_label,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{final_label} configured and enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.get("/{strategy_id}/configure-straddle")
def configure_straddle_form(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = "",
    underlying: str = "",
    expiry: str = "",
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    existing_params = existing.params if existing else {}
    underlying = (underlying or existing_params.get("underlying") or "NIFTY").upper()
    if underlying not in UNDERLYINGS:
        underlying = "NIFTY"
    if not expiry:
        expiry = existing_params.get("expiry") or ""

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    expiries: list[str] = []
    expiry_error: str | None = None
    atm_preview: dict | None = None
    selected_expiry = expiry

    if has_dhan:
        try:
            user_dhan = get_user_dhan_client(db, current_user)
            expiries = list_expiries(user_dhan.client, underlying)
        except DhanNotConnectedError as exc:
            expiry_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            expiry_error = f"Could not fetch expiries from Dhan: {exc}"

        if not selected_expiry and expiries:
            selected_expiry = expiries[0]

        if selected_expiry and selected_expiry in expiries:
            try:
                meta = UNDERLYINGS[underlying]
                chain_df, spot = fetch_chain_df(
                    user_dhan.client,
                    under_security_id=meta["security_id"],
                    expiry=selected_expiry,
                    under_exchange_segment=meta["exchange_segment"],
                )
                if not chain_df.empty:
                    strikes = sorted(chain_df["strike"].tolist())
                    atm_strike = min(strikes, key=lambda x: abs(x - spot))
                    row = chain_df[chain_df["strike"] == atm_strike].iloc[0]
                    ce_ltp = row.get("ce_ltp")
                    pe_ltp = row.get("pe_ltp")
                    atm_preview = {
                        "spot": spot,
                        "strike": atm_strike,
                        "ce_ltp": ce_ltp,
                        "pe_ltp": pe_ltp,
                        "combined": (float(ce_ltp) + float(pe_ltp)) if ce_ltp is not None and pe_ltp is not None else None,
                    }
            except Exception as exc:  # noqa: BLE001 — preview is a nice-to-have, never block the form
                expiry_error = expiry_error or f"Could not fetch live strikes: {exc}"

    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
        class_defaults = strategy_cls.default_params
    except ValueError:
        class_defaults = {}
    params = {**class_defaults, **strategy.default_params, **existing_params}
    params["underlying"] = underlying

    return render(
        request,
        "strategies/configure_atm_straddle.html",
        {
            "current_user": current_user,
            "user_strategy_id": user_strategy_id,
            "strategy": strategy,
            "has_dhan": has_dhan,
            "underlyings": UNDERLYINGS,
            "selected_underlying": underlying,
            "selected_expiry": selected_expiry,
            "expiries": expiries,
            "expiry_error": expiry_error,
            "atm_preview": atm_preview,
            "params": params,
            "existing": existing,
        },
    )


@router.post("/{strategy_id}/configure-straddle")
def configure_straddle_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = Form(""),
    label: str = Form(""),
    underlying: str = Form(...),
    expiry: str = Form(...),
    lots: int = Form(1),
    entry_time: str = Form("09:15"),
    close_time: str = Form("15:15"),
    reference_premium: float = Form(0),
    entry_trigger_mode: str = Form("pct"),
    entry_trigger_pct: float = Form(10),
    entry_trigger_flat: float = Form(0),
    leg_stop_loss_pct: float = Form(25),
    target_pct: float = Form(80),
    hedge_enabled: bool = Form(False),
    hedge_premium_target: float = Form(5),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if underlying.upper() not in UNDERLYINGS:
        flash(request, "Unknown underlying.", "error")
        return RedirectResponse(url(f"/strategies/{strategy_id}/configure-straddle"), status_code=303)

    if not expiry:
        flash(request, "Pick an expiry before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-straddle?underlying={underlying}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    if reference_premium <= 0:
        flash(request, "Set the combined CE+PE reference premium (from today's ATM prices) before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-straddle?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "underlying": underlying.upper(),
        "expiry": expiry,
        "lots": lots,
        "entry_time": entry_time,
        "close_time": close_time,
        "reference_premium": reference_premium,
        "entry_trigger_mode": "flat" if entry_trigger_mode == "flat" else "pct",
        "entry_trigger_pct": entry_trigger_pct,
        "entry_trigger_flat": entry_trigger_flat,
        "leg_stop_loss_pct": leg_stop_loss_pct,
        "target_pct": target_pct,
        "hedge_enabled": hedge_enabled,
        "hedge_premium_target": hedge_premium_target,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    final_label = label.strip() or f"ATM Straddle {params['underlying']}"

    if existing:
        existing.label = final_label
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                label=final_label,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{final_label} configured and enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.get("/{strategy_id}/configure-rolling-legsl")
def configure_rolling_legsl_form(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = "",
    underlying: str = "",
    expiry: str = "",
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    existing_params = existing.params if existing else {}
    underlying = (underlying or existing_params.get("underlying") or "NIFTY").upper()
    if underlying not in UNDERLYINGS:
        underlying = "NIFTY"
    if not expiry:
        expiry = existing_params.get("expiry") or ""

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
        class_defaults = strategy_cls.default_params
    except ValueError:
        class_defaults = {}
    params = {**class_defaults, **strategy.default_params, **existing_params}
    params["underlying"] = underlying
    # Same reasoning as configure-rolling: NIFTY/FINNIFTY trade in 50-point
    # strikes, BANKNIFTY/SENSEX in 100 — only re-default when the saved gap
    # wasn't actually saved for this underlying (switching the dropdown).
    if "strike_gap" not in existing_params or existing_params.get("underlying") != underlying:
        params["strike_gap"] = 100 if underlying in ("BANKNIFTY", "SENSEX") else 50

    expiries: list[str] = []
    expiry_error: str | None = None
    atm_preview: dict | None = None
    selected_expiry = expiry

    if has_dhan:
        try:
            user_dhan = get_user_dhan_client(db, current_user)
            expiries = list_expiries(user_dhan.client, underlying)
        except DhanNotConnectedError as exc:
            expiry_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            expiry_error = f"Could not fetch expiries from Dhan: {exc}"

        if not selected_expiry and expiries:
            selected_expiry = expiries[0]

        if selected_expiry and selected_expiry in expiries:
            try:
                meta = UNDERLYINGS[underlying]
                chain_df, spot = fetch_chain_df(
                    user_dhan.client,
                    under_security_id=meta["security_id"],
                    expiry=selected_expiry,
                    under_exchange_segment=meta["exchange_segment"],
                )
                if not chain_df.empty:
                    strikes = sorted(chain_df["strike"].tolist())
                    atm_strike = min(strikes, key=lambda x: abs(x - spot))
                    gap = float(params["strike_gap"] or 50)
                    top = min(strikes, key=lambda x: abs(x - (atm_strike + gap)))
                    bottom = min(strikes, key=lambda x: abs(x - (atm_strike - gap)))
                    atm_preview = {"spot": spot, "top": top, "middle": atm_strike, "bottom": bottom, "gap": gap}
            except Exception as exc:  # noqa: BLE001 — preview is a nice-to-have, never block the form
                expiry_error = expiry_error or f"Could not fetch live strikes: {exc}"

    return render(
        request,
        "strategies/configure_rolling_legsl.html",
        {
            "current_user": current_user,
            "user_strategy_id": user_strategy_id,
            "strategy": strategy,
            "has_dhan": has_dhan,
            "underlyings": UNDERLYINGS,
            "selected_underlying": underlying,
            "selected_expiry": selected_expiry,
            "expiries": expiries,
            "expiry_error": expiry_error,
            "atm_preview": atm_preview,
            "params": params,
            "existing": existing,
        },
    )


@router.post("/{strategy_id}/configure-rolling-legsl")
def configure_rolling_legsl_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = Form(""),
    label: str = Form(""),
    underlying: str = Form(...),
    expiry: str = Form(...),
    lots: int = Form(1),
    start_time: str = Form("09:20"),
    end_time: str = Form("14:45"),
    strike_gap: float = Form(50),
    leg_stop_loss_pct: float = Form(25),
    leg_target_pct: float = Form(80),
    daily_stop_loss: float = Form(10000),
    daily_target: float = Form(15000),
    hedge_enabled: bool = Form(False),
    hedge_premium_target: float = Form(5),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if underlying.upper() not in UNDERLYINGS:
        flash(request, "Unknown underlying.", "error")
        return RedirectResponse(url(f"/strategies/{strategy_id}/configure-rolling-legsl"), status_code=303)

    if not expiry:
        flash(request, "Pick an expiry before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling-legsl?underlying={underlying}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    if strike_gap <= 0:
        flash(request, "Strike gap must be greater than zero.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling-legsl?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    # Leg SL/Target are meant to be one of exactly two choices each — the
    # <select> on the form already constrains this, this just guards a
    # hand-crafted POST from saving something the strategy wasn't designed
    # to offer as a "pick one of these" input.
    if leg_stop_loss_pct not in (25, 30):
        flash(request, "Leg Stop Loss must be 25% or 30%.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling-legsl?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)
    if leg_target_pct not in (70, 80):
        flash(request, "Leg Target must be 70% or 80%.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-rolling-legsl?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "underlying": underlying.upper(),
        "expiry": expiry,
        "lots": lots,
        "start_time": start_time,
        "end_time": end_time,
        "strike_gap": strike_gap,
        "leg_stop_loss_pct": leg_stop_loss_pct,
        "leg_target_pct": leg_target_pct,
        "daily_stop_loss": daily_stop_loss,
        "daily_target": daily_target,
        "hedge_enabled": hedge_enabled,
        "hedge_premium_target": hedge_premium_target,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    final_label = label.strip() or f"3-Pair Rolling Leg-SL {params['underlying']}"

    if existing:
        existing.label = final_label
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                label=final_label,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{final_label} configured and enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


@router.get("/{strategy_id}/configure-iron-condor")
def configure_iron_condor_form(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = "",
    underlying: str = "",
    expiry: str = "",
    expiry_type: str = "",
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    existing_params = existing.params if existing else {}
    underlying = (underlying or existing_params.get("underlying") or "NIFTY").upper()
    if underlying not in UNDERLYINGS:
        underlying = "NIFTY"
    expiry_type = (expiry_type or existing_params.get("expiry_type") or "weekly").lower()
    if expiry_type not in ("weekly", "monthly"):
        expiry_type = "weekly"
    if not expiry:
        expiry = existing_params.get("expiry") or ""

    has_dhan = current_user.dhan_credential is not None and current_user.dhan_credential.is_active

    try:
        strategy_cls = get_strategy_class(strategy.code_ref)
        class_defaults = strategy_cls.default_params
    except ValueError:
        class_defaults = {}
    params = {**class_defaults, **strategy.default_params, **existing_params}
    params["underlying"] = underlying
    params["expiry_type"] = expiry_type

    all_expiries: list[str] = []
    expiries: list[str] = []
    expiry_error: str | None = None
    condor_preview: dict | None = None
    selected_expiry = expiry

    if has_dhan:
        try:
            user_dhan = get_user_dhan_client(db, current_user)
            all_expiries = list_expiries(user_dhan.client, underlying)
        except DhanNotConnectedError as exc:
            expiry_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            expiry_error = f"Could not fetch expiries from Dhan: {exc}"

        expiry_labels = _classify_expiries(all_expiries)
        expiries = [e for e in all_expiries if expiry_labels.get(e) == expiry_type]

        if selected_expiry not in expiries:
            selected_expiry = expiries[0] if expiries else ""

        if selected_expiry and selected_expiry in expiries:
            try:
                meta = UNDERLYINGS[underlying]
                chain_df, spot = fetch_chain_df(
                    user_dhan.client,
                    under_security_id=meta["security_id"],
                    expiry=selected_expiry,
                    under_exchange_segment=meta["exchange_segment"],
                )
                if not chain_df.empty:
                    strikes = sorted(chain_df["strike"].tolist())
                    sell_offset = float(params.get("sell_offset_points") or 250)
                    buy_offset = float(params.get("buy_offset_points") or 350)
                    ceb = min(strikes, key=lambda x: abs(x - (spot + buy_offset)))
                    ces = min(strikes, key=lambda x: abs(x - (spot + sell_offset)))
                    pes = min(strikes, key=lambda x: abs(x - (spot - sell_offset)))
                    peb = min(strikes, key=lambda x: abs(x - (spot - buy_offset)))
                    condor_preview = {"spot": spot, "ceb": ceb, "ces": ces, "pes": pes, "peb": peb}
            except Exception as exc:  # noqa: BLE001 — preview is a nice-to-have, never block the form
                expiry_error = expiry_error or f"Could not fetch live strikes: {exc}"

    return render(
        request,
        "strategies/configure_iron_condor.html",
        {
            "current_user": current_user,
            "user_strategy_id": user_strategy_id,
            "strategy": strategy,
            "has_dhan": has_dhan,
            "underlyings": UNDERLYINGS,
            "selected_underlying": underlying,
            "selected_expiry_type": expiry_type,
            "selected_expiry": selected_expiry,
            "expiries": expiries,
            "expiry_error": expiry_error,
            "condor_preview": condor_preview,
            "params": params,
            "existing": existing,
        },
    )


@router.post("/{strategy_id}/configure-iron-condor")
def configure_iron_condor_submit(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    strategy_id: uuid.UUID,
    user_strategy_id: str = Form(""),
    label: str = Form(""),
    underlying: str = Form(...),
    expiry_type: str = Form("weekly"),
    expiry: str = Form(...),
    lots: int = Form(1),
    start_time: str = Form("09:20"),
    end_time: str = Form("14:45"),
    sell_offset_points: float = Form(250),
    buy_offset_points: float = Form(350),
    sl_target_mode: str = Form("fixed"),
    stop_loss_value: float = Form(10000),
    target_value: float = Form(15000),
    order_type: str = Form("LIMIT"),
    live_confirmed: bool = Form(False),
    mode: str = Form("paper"),
):
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or not strategy.is_published:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if not current_user.dhan_credential or not current_user.dhan_credential.is_active:
        flash(request, "Connect your Dhan account on the Settings page before enabling a strategy.", "error")
        return RedirectResponse(url("/strategies"), status_code=303)

    if underlying.upper() not in UNDERLYINGS:
        flash(request, "Unknown underlying.", "error")
        return RedirectResponse(url(f"/strategies/{strategy_id}/configure-iron-condor"), status_code=303)

    if not expiry:
        flash(request, "Pick an expiry before saving.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-iron-condor?underlying={underlying}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    if sell_offset_points <= 0 or buy_offset_points <= sell_offset_points:
        flash(request, "Buy Wing Offset must be greater than Sell Strike Offset, and both must be positive.", "error")
        redirect_url = f"/strategies/{strategy_id}/configure-iron-condor?underlying={underlying}&expiry={expiry}"
        if user_strategy_id:
            redirect_url += f"&user_strategy_id={user_strategy_id}"
        return RedirectResponse(url(redirect_url), status_code=303)

    requested_mode = _resolve_requested_mode(request, mode, live_confirmed)

    params = {
        "underlying": underlying.upper(),
        "expiry_type": "monthly" if expiry_type == "monthly" else "weekly",
        "expiry": expiry,
        "lots": lots,
        "start_time": start_time,
        "end_time": end_time,
        "sell_offset_points": sell_offset_points,
        "buy_offset_points": buy_offset_points,
        "sl_target_mode": "pct" if sl_target_mode == "pct" else "fixed",
        "stop_loss_value": stop_loss_value,
        "target_value": target_value,
        "order_type": "MARKET" if order_type == "MARKET" else "LIMIT",
    }

    existing: UserStrategy | None = None
    if user_strategy_id:
        try:
            existing = db.get(UserStrategy, uuid.UUID(user_strategy_id))
        except ValueError:
            existing = None
        if existing is None or existing.user_id != current_user.id or existing.strategy_id != strategy_id:
            flash(request, "Strategy instance not found.", "error")
            return RedirectResponse(url("/strategies"), status_code=303)

    final_label = label.strip() or f"Iron Condor Rolling {params['underlying']}"

    if existing:
        existing.label = final_label
        existing.params = params
        existing.mode = requested_mode
        existing.is_active = True
    else:
        db.add(
            UserStrategy(
                user_id=current_user.id,
                strategy_id=strategy_id,
                label=final_label,
                params=params,
                mode=requested_mode,
                is_active=True,
            )
        )
    db.commit()

    flash(request, f"{final_label} configured and enabled in {requested_mode.value} mode.", "success")
    return RedirectResponse(url("/strategies"), status_code=303)


# --- Superadmin: publish/manage strategy definitions ---


@router.get("/admin")
def admin_strategies(request: Request, db: DbSession, current_user: SuperadminUser):
    all_strategies = db.scalars(select(Strategy)).all()
    return render(
        request,
        "strategies/admin.html",
        {
            "current_user": current_user,
            "strategies": all_strategies,
            "registry_keys": list(STRATEGY_REGISTRY.keys()),
        },
    )


@router.post("/admin/create")
def admin_create_strategy(
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
    name: str = Form(...),
    description: str = Form(""),
    code_ref: str = Form(...),
    default_params_json: str = Form("{}"),
):
    try:
        get_strategy_class(code_ref)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url("/strategies/admin"), status_code=303)

    try:
        default_params = json.loads(default_params_json or "{}")
    except json.JSONDecodeError:
        flash(request, "Default params must be valid JSON.", "error")
        return RedirectResponse(url("/strategies/admin"), status_code=303)

    db.add(
        Strategy(
            name=name,
            description=description,
            code_ref=code_ref,
            config_schema={},
            default_params=default_params,
            is_published=False,
        )
    )
    db.commit()
    flash(request, "Strategy created (unpublished). Review and publish it below.", "success")
    return RedirectResponse(url("/strategies/admin"), status_code=303)


@router.post("/admin/{strategy_id}/toggle-publish")
def admin_toggle_publish(request: Request, db: DbSession, current_user: SuperadminUser, strategy_id: uuid.UUID):
    strategy = db.get(Strategy, strategy_id)
    if strategy:
        strategy.is_published = not strategy.is_published
        db.commit()
        flash(request, f"{strategy.name} is now {'published' if strategy.is_published else 'unpublished'}.", "success")
    return RedirectResponse(url("/strategies/admin"), status_code=303)


@router.post("/admin/{strategy_id}/delete")
def admin_delete_strategy(request: Request, db: DbSession, current_user: SuperadminUser, strategy_id: uuid.UUID):
    """Permanently remove a strategy definition and every user's instance
    of it (across all users, not just the superadmin) — blocked if any of
    those instances has an open position, so nothing vanishes unresolved."""
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        flash(request, "Strategy not found.", "error")
        return RedirectResponse(url("/strategies/admin"), status_code=303)

    instances = db.scalars(select(UserStrategy).where(UserStrategy.strategy_id == strategy_id)).all()
    for us in instances:
        if find_open_run(us) is not None:
            flash(
                request,
                f"Cannot delete {strategy.name} — a user still has an open position on "
                f"{us.label or strategy.name}. It must be closed first.",
                "error",
            )
            return RedirectResponse(url("/strategies/admin"), status_code=303)

    name = strategy.name
    for us in instances:
        db.delete(us)  # cascades to that instance's runs/orders
    db.delete(strategy)
    db.commit()

    suffix = f" and {len(instances)} user instance(s)" if instances else ""
    flash(request, f"{name} deleted{suffix}.", "success")
    return RedirectResponse(url("/strategies/admin"), status_code=303)
