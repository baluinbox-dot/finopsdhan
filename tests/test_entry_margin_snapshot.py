"""StrategyRun.entry_margin: a snapshot of combined margin blocked, taken
once right after entry (app.engine.runner._execute_entry) so a report
built after the run has already closed (the daily summary email) still
has a margin figure -- margin is otherwise only ever computed live for a
currently-open run (app.engine.pnl.compute_combined_margin)."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.engine.runner import enter_user_strategy_now, find_open_run
from app.models import Strategy, StrategyMode, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg


def _make_user_strategy(db_session) -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True, params={})
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def test_entry_captures_combined_margin_snapshot(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    fake_leg = OrderLeg(
        label="SELL X", security_id="1", trading_symbol="X", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )
    dhan = MagicMock()
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: [fake_leg])
    monkeypatch.setattr("app.engine.runner._place_or_paper_leg", lambda *a, **k: MagicMock())
    monkeypatch.setattr("app.engine.runner.fetch_combined_margin", lambda client, legs: 38500.0)

    assert enter_user_strategy_now(db_session, user_strategy) is True

    run = find_open_run(user_strategy)
    assert run is not None
    assert run.entry_margin == 38500.0


def test_entry_margin_is_none_not_zero_when_fetch_fails(db_session, monkeypatch):
    """fetch_combined_margin already returns None (never raises) on any
    failure -- confirms that None survives onto the run rather than
    silently becoming 0, which a report must never read as "no margin
    used"."""
    user_strategy = _make_user_strategy(db_session)
    fake_leg = OrderLeg(
        label="SELL X", security_id="1", trading_symbol="X", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )
    dhan = MagicMock()
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: [fake_leg])
    monkeypatch.setattr("app.engine.runner._place_or_paper_leg", lambda *a, **k: MagicMock())
    monkeypatch.setattr("app.engine.runner.fetch_combined_margin", lambda client, legs: None)

    assert enter_user_strategy_now(db_session, user_strategy) is True

    run = find_open_run(user_strategy)
    assert run.entry_margin is None
