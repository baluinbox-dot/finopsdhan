"""End-of-day email: for every strategy instance that traded today, its
label, mode (Live/Paper), margin used, and today's P&L — sent once daily
(see app.engine.scheduler) so Balu has a single message to read off when
manually posting a performance update to social media, rather than
piecing it together from the Dashboard himself.

Deliberately separate from app.engine.pnl (which is Dashboard-facing,
"what's open right now") -- this is "what happened today," including
runs that have already closed by the time it's built.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.dhan.client import DhanNotConnectedError, get_user_dhan_client
from app.dhan.helpers import fetch_quotes
from app.email import send_email
from app.models import StrategyRun, User, UserStrategy
from app.strategies.base import currently_open_legs, leg_pnl
from app.templating import IST, to_ist

logger = logging.getLogger("app.engine.daily_summary")


def _run_day_pnl(dhan_client: Any | None, run: StrategyRun) -> tuple[float, bool]:
    """(pnl, fully_priced) for one run's contribution to today's total:
    realized P&L booked so far, plus live mark-to-market on anything still
    open -- mirrors app.engine.pnl.compute_live_pnl's own math on purpose,
    so this number and the Dashboard's never quietly disagree. A closed
    run's realized_pnl is already final. `fully_priced` is False whenever
    the run is still open and either no Dhan client is available or a
    fresh quote couldn't be fetched for something still open -- the
    caller should treat the total as a lower bound in that case, not a
    final number."""
    realized_so_far = float(run.realized_pnl or 0)
    if run.status == "closed":
        return realized_so_far, True

    legs_planned = run.legs_planned or {}
    all_legs = legs_planned.get("legs") or []
    leg_state = legs_planned.get("leg_state") or {}
    open_legs = currently_open_legs(all_legs, leg_state)
    if not open_legs:
        return realized_so_far, True

    if dhan_client is None:
        return realized_so_far, False

    securities_by_segment: dict[str, list[int]] = {}
    for leg in open_legs:
        securities_by_segment.setdefault(leg["exchange_segment"], []).append(int(leg["security_id"]))
    quotes = fetch_quotes(dhan_client, securities_by_segment)

    unrealized = 0.0
    for leg in open_legs:
        quote = quotes.get((leg["exchange_segment"], str(leg["security_id"])))
        if quote is None:
            return realized_so_far, False
        unrealized += leg_pnl(leg, float(quote.get("last_price", 0)))
    return realized_so_far + unrealized, True


def build_user_daily_summary(db: Session, user: User) -> dict[str, Any] | None:
    """One row per UserStrategy instance that had at least one StrategyRun
    start today (IST calendar date) -- idle instances that didn't trade
    are left out entirely, same reasoning as the Dashboard's own
    hide-idle-instances behavior. Returns None when nothing traded today
    (weekend, holiday, or just no entry conditions met) so the caller can
    skip sending an empty email."""
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()

    user_strategies = db.scalars(
        select(UserStrategy)
        .where(UserStrategy.user_id == user.id)
        .options(selectinload(UserStrategy.strategy), selectinload(UserStrategy.runs))
    ).all()

    try:
        dhan_client = get_user_dhan_client(db, user).client
    except DhanNotConnectedError:
        dhan_client = None

    rows: list[dict[str, Any]] = []
    for us in user_strategies:
        todays_runs = sorted(
            (r for r in us.runs if to_ist(r.started_at).date() == today_ist),
            key=lambda r: r.started_at,
        )
        if not todays_runs:
            continue

        total_pnl = 0.0
        fully_priced = True
        margin_used: float | None = None
        still_open = False
        for run in todays_runs:
            pnl, priced = _run_day_pnl(dhan_client, run)
            total_pnl += pnl
            fully_priced = fully_priced and priced
            if run.entry_margin is not None:
                margin_used = float(run.entry_margin)  # latest run today wins
            if run.status != "closed":
                still_open = True

        rows.append({
            "label": us.label or us.strategy.name,
            "mode": us.mode.value,
            "margin_used": margin_used,
            "pnl": total_pnl,
            "fully_priced": fully_priced,
            "still_open": still_open,
        })

    if not rows:
        return None

    rows.sort(key=lambda r: r["label"].lower())
    return {
        "date": today_ist,
        "rows": rows,
        "total_pnl": sum(r["pnl"] for r in rows),
        "any_unpriced": any(not r["fully_priced"] for r in rows),
    }


def _fmt_rupees(value: float | None) -> str:
    if value is None:
        return "—"
    return f"-₹{abs(value):,.0f}" if value < 0 else f"₹{value:,.0f}"


def _render_email(user: User, summary: dict[str, Any]) -> tuple[str, str, str]:
    """(subject, html_body, text_body)."""
    settings = get_settings()
    date_str = summary["date"].strftime("%d %b %Y")
    subject = f"FinOps Algo — Daily Strategy Summary — {date_str}"

    html_rows = []
    text_rows = []
    for r in summary["rows"]:
        status_note = " (still open)" if r["still_open"] else ""
        unpriced_note = " — partial, unpriced leg" if r["still_open"] and not r["fully_priced"] else ""
        html_rows.append(
            "<tr>"
            f"<td>{r['label']}{status_note}</td>"
            f"<td>{r['mode'].title()}</td>"
            f"<td>{_fmt_rupees(r['margin_used'])}</td>"
            f"<td>{_fmt_rupees(r['pnl'])}{unpriced_note}</td>"
            "</tr>"
        )
        text_rows.append(
            f"- {r['label']}{status_note} | {r['mode'].title()} | "
            f"Margin {_fmt_rupees(r['margin_used'])} | P&L {_fmt_rupees(r['pnl'])}{unpriced_note}"
        )

    footer_lines_html = []
    footer_lines_text = []
    if settings.public_app_url:
        footer_lines_html.append(f"<p>{settings.public_app_url}</p>")
        footer_lines_text.append(settings.public_app_url)
    footer_lines_html.append(f"<p>Interested? Reach out — {user.email}</p>")
    footer_lines_text.append(f"Interested? Reach out — {user.email}")

    unpriced_notice = (
        "<p><em>One or more still-open positions couldn't be freshly priced — "
        "totals above are a lower bound, not final.</em></p>"
        if summary["any_unpriced"] else ""
    )
    unpriced_notice_text = (
        "\nNote: one or more still-open positions couldn't be freshly priced — "
        "totals above are a lower bound, not final.\n"
        if summary["any_unpriced"] else ""
    )

    html_body = (
        f"<h3>Daily Strategy Summary — {date_str}</h3>"
        "<table cellpadding='6' style='border-collapse:collapse' border='1'>"
        "<tr><th>Strategy</th><th>Mode</th><th>Margin Used</th><th>P&amp;L</th></tr>"
        + "".join(html_rows) +
        "</table>"
        f"<p><strong>Total P&amp;L: {_fmt_rupees(summary['total_pnl'])}</strong></p>"
        + unpriced_notice
        + "".join(footer_lines_html)
    )
    text_body = (
        f"Daily Strategy Summary — {date_str}\n\n"
        + "\n".join(text_rows)
        + f"\n\nTotal P&L: {_fmt_rupees(summary['total_pnl'])}\n"
        + unpriced_notice_text
        + "\n" + "\n".join(footer_lines_text)
    )
    return subject, html_body, text_body


def send_daily_summary_email(db: Session, user: User) -> bool:
    """Build and send today's summary to `user`'s own registered email.
    Returns False (no email sent) when nothing traded today — deliberately
    silent rather than an empty "nothing happened" email every weekend."""
    summary = build_user_daily_summary(db, user)
    if summary is None:
        return False
    subject, html_body, text_body = _render_email(user, summary)
    return send_email(user.email, subject, html_body=html_body, text_body=text_body)


def send_daily_summaries_for_all_users(db: Session) -> int:
    """Called once daily by the scheduler (see app.engine.scheduler) for
    every user who owns at least one strategy instance. Returns how many
    emails actually went out (skips users with nothing traded today, and
    never lets one user's failure stop the rest)."""
    user_ids = db.scalars(select(UserStrategy.user_id).distinct()).all()
    sent = 0
    for user_id in user_ids:
        user = db.get(User, user_id)
        if user is None:
            continue
        try:
            if send_daily_summary_email(db, user):
                sent += 1
        except Exception:  # noqa: BLE001 — one user's failure must never block the rest
            logger.exception("Failed to send daily summary to %s", user.email)
    return sent
