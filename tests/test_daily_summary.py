"""app.engine.daily_summary: the end-of-day email listing every strategy
instance that traded today, its mode, margin used, and today's P&L."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.dhan.client import DhanNotConnectedError
from app.engine import daily_summary
from app.engine.daily_summary import (
    build_user_daily_summary,
    send_daily_summaries_for_all_users,
    send_daily_summary_email,
)
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy


def _sell_leg(sid: str, price: float, quantity: int = 75) -> dict:
    return {
        "label": "SELL X", "security_id": sid, "trading_symbol": "X", "exchange_segment": "NSE_FNO",
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "primary",
    }


def _make_user(db_session, email: str = "trader@example.com") -> User:
    user = User(email=email, password_hash="x", role=UserRole.USER)
    db_session.add(user)
    db_session.flush()
    return user


def _make_instance(db_session, user, *, label="NIFTY Strangle", mode=StrategyMode.PAPER, strategy_name="Test Strategy") -> UserStrategy:
    strategy = Strategy(name=strategy_name, code_ref="example_short_strangle", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    us = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=mode, is_active=True, label=label)
    db_session.add(us)
    db_session.flush()
    return us


def _add_run(db_session, us, *, started_at, status="closed", realized_pnl=0.0, entry_margin=None, legs=None) -> StrategyRun:
    run = StrategyRun(
        user_strategy_id=us.id,
        started_at=started_at,
        status=status,
        realized_pnl=realized_pnl,
        entry_margin=entry_margin,
        legs_planned={"legs": legs or []},
        closed_at=started_at if status == "closed" else None,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _no_dhan(monkeypatch):
    monkeypatch.setattr(
        daily_summary, "get_user_dhan_client",
        MagicMock(side_effect=DhanNotConnectedError("no dhan")),
    )


def _now():
    return datetime.now(timezone.utc)


def _yesterday():
    return _now() - timedelta(days=1)


# --- build_user_daily_summary ---


def test_returns_none_when_nothing_traded_today(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    _make_instance(db_session, user)  # configured, but no runs at all

    assert build_user_daily_summary(db_session, user) is None


def test_excludes_a_run_from_a_different_day(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    us = _make_instance(db_session, user)
    _add_run(db_session, us, started_at=_yesterday(), status="closed", realized_pnl=500.0)

    assert build_user_daily_summary(db_session, user) is None


def test_closed_run_reports_realized_pnl_margin_and_mode(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    us = _make_instance(db_session, user, label="NIFTY Iron Fly", mode=StrategyMode.LIVE)
    _add_run(db_session, us, started_at=_now(), status="closed", realized_pnl=1250.0, entry_margin=45000.0)

    summary = build_user_daily_summary(db_session, user)

    assert summary is not None
    assert summary["total_pnl"] == 1250.0
    assert summary["any_unpriced"] is False
    row = summary["rows"][0]
    assert row["label"] == "NIFTY Iron Fly"
    assert row["mode"] == "live"
    assert row["margin_used"] == 45000.0
    assert row["pnl"] == 1250.0
    assert row["still_open"] is False
    assert row["fully_priced"] is True


def test_idle_instance_with_no_runs_today_is_left_out_of_a_nonempty_summary(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    traded = _make_instance(db_session, user, label="Traded Today", strategy_name="A")
    idle = _make_instance(db_session, user, label="Never Ran", strategy_name="B")
    _add_run(db_session, traded, started_at=_now(), status="closed", realized_pnl=100.0)
    del idle

    summary = build_user_daily_summary(db_session, user)

    labels = [r["label"] for r in summary["rows"]]
    assert labels == ["Traded Today"]


def test_aggregates_multiple_runs_of_the_same_instance_today(db_session, monkeypatch):
    """Two closed runs today (e.g. Close Now then Enter Now again) sum
    their P&L; margin_used takes the *latest* run's snapshot."""
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    us = _make_instance(db_session, user)
    now = _now()
    _add_run(db_session, us, started_at=now - timedelta(hours=3), status="closed", realized_pnl=800.0, entry_margin=40000.0)
    _add_run(db_session, us, started_at=now - timedelta(hours=1), status="closed", realized_pnl=-200.0, entry_margin=42000.0)

    summary = build_user_daily_summary(db_session, user)

    assert len(summary["rows"]) == 1
    row = summary["rows"][0]
    assert row["pnl"] == 600.0  # 800 - 200
    assert row["margin_used"] == 42000.0  # the later run's snapshot wins


def test_still_open_run_adds_live_unrealized_pnl(db_session, monkeypatch):
    user = _make_user(db_session)
    us = _make_instance(db_session, user)
    _add_run(
        db_session, us, started_at=_now(), status="open", realized_pnl=100.0, entry_margin=30000.0,
        legs=[_sell_leg("1", 50.0)],
    )

    dhan_client = MagicMock()
    dhan_client.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 20.0}}}},
    }
    monkeypatch.setattr(daily_summary, "get_user_dhan_client", MagicMock(return_value=MagicMock(client=dhan_client)))

    summary = build_user_daily_summary(db_session, user)

    row = summary["rows"][0]
    # realized 100 + unrealized (50-20)*75 = 100 + 2250 = 2350
    assert row["pnl"] == 2350.0
    assert row["still_open"] is True
    assert row["fully_priced"] is True
    assert summary["any_unpriced"] is False


def test_still_open_run_with_no_dhan_connection_is_flagged_unpriced(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    us = _make_instance(db_session, user)
    _add_run(
        db_session, us, started_at=_now(), status="open", realized_pnl=100.0,
        legs=[_sell_leg("1", 50.0)],
    )

    summary = build_user_daily_summary(db_session, user)

    row = summary["rows"][0]
    assert row["pnl"] == 100.0  # realized only -- no live price available
    assert row["fully_priced"] is False
    assert summary["any_unpriced"] is True


# --- send_daily_summary_email ---


def test_send_email_returns_false_and_sends_nothing_when_summary_is_empty(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session)
    sent = MagicMock()
    monkeypatch.setattr(daily_summary, "send_email", sent)

    assert send_daily_summary_email(db_session, user) is False
    sent.assert_not_called()


def test_send_email_includes_label_mode_margin_pnl_contact_line_disclaimer_and_automation_note(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    user = _make_user(db_session, email="balu@example.com")
    us = _make_instance(db_session, user, label="SENSEX Dynamic Strangle", mode=StrategyMode.LIVE)
    _add_run(db_session, us, started_at=_now(), status="closed", realized_pnl=-350.0, entry_margin=60000.0)

    captured = {}

    def _fake_send_email(to_email, subject, *, html_body, text_body):
        captured.update(to_email=to_email, subject=subject, html_body=html_body, text_body=text_body)
        return True

    monkeypatch.setattr(daily_summary, "send_email", _fake_send_email)

    result = send_daily_summary_email(db_session, user)

    assert result is True
    assert captured["to_email"] == "balu@example.com"
    assert "Daily Strategy Summary" in captured["subject"]
    assert "SENSEX Dynamic Strangle" in captured["text_body"]
    assert "Live" in captured["text_body"]
    assert "60,000" in captured["text_body"]
    assert "-₹350" in captured["text_body"]
    assert "balu@example.com" in captured["text_body"]
    assert "Disclaimer : Personal Trades | For transparency only | No advice or recommendations." in captured["text_body"]
    assert "http://" not in captured["text_body"] and "https://" not in captured["text_body"]
    assert "Fully Automated — No Manual Intervention" in captured["text_body"]
    # Automation note sits at the top, disclaimer at the bottom -- not folded together.
    text = captured["text_body"]
    assert text.index("Fully Automated") < text.index("SENSEX Dynamic Strangle") < text.index("Disclaimer :")


# --- send_daily_summaries_for_all_users ---


def test_send_all_skips_users_with_nothing_and_survives_one_users_failure(db_session, monkeypatch):
    _no_dhan(monkeypatch)
    quiet_user = _make_user(db_session, email="quiet@example.com")
    _make_instance(db_session, quiet_user)  # never traded

    broken_user = _make_user(db_session, email="broken@example.com")
    broken_us = _make_instance(db_session, broken_user, label="Broken")
    _add_run(db_session, broken_us, started_at=_now(), status="closed", realized_pnl=10.0)

    good_user = _make_user(db_session, email="good@example.com")
    good_us = _make_instance(db_session, good_user, label="Good")
    _add_run(db_session, good_us, started_at=_now(), status="closed", realized_pnl=20.0)

    def _fake_send_email(to_email, subject, *, html_body, text_body):
        if to_email == "broken@example.com":
            raise RuntimeError("SMTP exploded")
        return True

    monkeypatch.setattr(daily_summary, "send_email", _fake_send_email)

    sent = send_daily_summaries_for_all_users(db_session)

    assert sent == 1  # only good_user's email actually went out
