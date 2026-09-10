"""Every real entry and close now emails the strategy owner (Balu's
request: "every new order entry and close order need email alerts with
strategies name") -- covers _send_entry_alert/_send_close_alert and their
wiring into all four call sites: run_user_strategy's scheduled entry and
scheduled exit, and the manual Enter Now / Close Now buttons. No cooldown
here (unlike the error alert) -- each is a real, infrequent trading event,
not a poll-tick condition that could repeat every 30s."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine import runner
from app.engine.runner import close_user_strategy_now, enter_user_strategy_now, run_user_strategy
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg


def _make_user_strategy(db_session, *, mode: StrategyMode = StrategyMode.PAPER, code_ref: str = "example_short_strangle") -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref=code_ref, is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=mode, is_active=True, params={})
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def _capture_emails(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(
        runner, "send_email",
        lambda to_email, subject, *, html_body, text_body: sent.append((to_email, subject, html_body, text_body)),
    )
    return sent


def _sell_leg(price: float = 50.0) -> dict:
    return {
        "label": "SELL X", "security_id": "1", "trading_symbol": "X 25000 CE", "exchange_segment": "NSE_FNO",
        "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT", "product_type": "INTRADAY",
        "price": price, "role": "primary",
    }


def _make_open_run(db_session, user_strategy: UserStrategy, *, sell_price: float = 50.0) -> StrategyRun:
    run = StrategyRun(
        user_strategy_id=user_strategy.id, started_at=datetime.now(timezone.utc), status="open",
        legs_planned={"legs": [_sell_leg(sell_price)], "entry_premium": sell_price},
    )
    db_session.add(run)
    db_session.commit()
    return run


# --- entry alerts ---


def test_scheduled_entry_sends_an_alert_email(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    sent = _capture_emails(monkeypatch)
    fake_leg = OrderLeg(
        label="SELL 25000 CE", security_id="1", trading_symbol="NIFTY 25000 CE", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=MagicMock()))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: [fake_leg])
    monkeypatch.setattr(runner, "_place_or_paper_leg", lambda *a, **k: MagicMock())
    monkeypatch.setattr(runner, "fetch_combined_margin", lambda client, legs: 12345.0)

    run_user_strategy(db_session, user_strategy)

    assert len(sent) == 1
    to_email, subject, html_body, text_body = sent[0]
    assert to_email == "trader@example.com"
    assert "Entered" in subject and "Test Strategy" in subject and "PAPER" in subject
    assert "SELL 75" in text_body and "NIFTY 25000 CE" in text_body
    assert "12,345" in text_body


def test_enter_now_sends_an_alert_email(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session, mode=StrategyMode.LIVE)
    sent = _capture_emails(monkeypatch)
    fake_leg = OrderLeg(
        label="SELL X", security_id="1", trading_symbol="X", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=MagicMock()))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: [fake_leg])
    monkeypatch.setattr(runner, "_place_or_paper_leg", lambda *a, **k: MagicMock())
    monkeypatch.setattr(runner, "fetch_combined_margin", lambda client, legs: None)

    # LIVE mode but ALLOW_LIVE_TRADING is off in the test settings (conftest
    # sets it false) -- is_live still computes False, only the subject's
    # mode label is exercised here, not real order placement.
    assert enter_user_strategy_now(db_session, user_strategy) is True

    assert len(sent) == 1
    _, subject, _, text_body = sent[0]
    assert "Entered" in subject and "Test Strategy" in subject
    assert "Margin used: unknown" in text_body  # None -> "unknown", never "Rs 0"


def test_no_entry_alert_when_conditions_are_not_met(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    sent = _capture_emails(monkeypatch)
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=MagicMock()))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: None)

    run_user_strategy(db_session, user_strategy)

    assert sent == []


# --- close alerts ---


def test_scheduled_exit_sends_an_alert_email(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    run = _make_open_run(db_session, user_strategy, sell_price=50.0)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 30.0}}}},
    }
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_exit", lambda self, ctx, notes: True)
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_leg_exits", lambda self, ctx, notes: None)
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_rolls", lambda self, ctx, notes: None)

    run_user_strategy(db_session, user_strategy)

    db_session.refresh(run)
    assert run.status == "closed"
    assert len(sent) == 1
    to_email, subject, html_body, text_body = sent[0]
    assert to_email == "trader@example.com"
    assert "Closed" in subject and "Test Strategy" in subject and "PAPER" in subject
    assert "Profit" in subject  # sold @50, bought back @30 -> profit
    assert "1,500" in text_body  # (50-30)*75


def test_close_now_sends_an_alert_email(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    run = _make_open_run(db_session, user_strategy, sell_price=50.0)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 70.0}}}},
    }
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    assert close_user_strategy_now(db_session, user_strategy) is True

    db_session.refresh(run)
    assert len(sent) == 1
    to_email, subject, html_body, text_body = sent[0]
    assert "Closed" in subject and "Loss" in subject  # sold @50, bought back @70 -> loss
    assert "Manually closed by user." in text_body


def test_close_now_does_not_alert_when_quote_fetch_fails(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    _make_open_run(db_session, user_strategy, sell_price=50.0)
    sent = _capture_emails(monkeypatch)
    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {}}}
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    try:
        close_user_strategy_now(db_session, user_strategy)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    assert sent == []


def test_a_failed_close_alert_send_never_propagates(db_session, monkeypatch):
    """Same defensiveness contract as the existing error-alert test: a
    broken mail integration must never turn a real successful close into
    a user-visible crash."""
    user_strategy = _make_user_strategy(db_session)
    run = _make_open_run(db_session, user_strategy, sell_price=50.0)
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 30.0}}}},
    }
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    def _raise_email(*a, **k):
        raise RuntimeError("SMTP is on fire")

    monkeypatch.setattr(runner, "send_email", _raise_email)

    assert close_user_strategy_now(db_session, user_strategy) is True  # must not raise
    db_session.refresh(run)
    assert run.status == "closed"
