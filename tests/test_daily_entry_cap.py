"""Once a strategy instance closes today (automatically or via manual
Close Now), the scheduler will not re-enter it again on its own — that's
_today_run_count counting every run regardless of how it ended. The one
deliberate way around that same day is the manual "Enter Now" action
(enter_user_strategy_now), which bypasses the count for a single
on-demand attempt but still requires the strategy's real entry
conditions to actually be met."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.engine.runner import (
    _close_open_run,
    _today_run_count,
    close_user_strategy_now,
    enter_user_strategy_now,
    find_open_run,
)
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg


def _make_user_strategy(db_session, *, code_ref: str = "example_short_strangle", params: dict | None = None) -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref=code_ref, is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True, params=params or {},
    )
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def _add_run(db_session, user_strategy: UserStrategy, *, status: str, manually_closed: bool) -> StrategyRun:
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status=status,
        manually_closed=manually_closed,
        legs_planned={"legs": [], "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)
    return run


# --- _today_run_count: every run today counts, regardless of how it ended ---


def test_automatically_closed_run_counts_toward_daily_cap(db_session):
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=False)
    assert _today_run_count(user_strategy) == 1


def test_manually_closed_run_also_counts_toward_daily_cap(db_session):
    """Reverted from an earlier attempt: a manual Close Now should stop
    the scheduler from auto-re-entering today just like a real exit does —
    only the explicit Enter Now action can override that."""
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=True)
    assert _today_run_count(user_strategy) == 1


def test_close_user_strategy_now_still_records_manually_closed_flag(db_session, monkeypatch):
    """manually_closed is still tracked (useful metadata for e.g. a future
    trade-history view) even though it no longer affects the daily cap."""
    user_strategy = _make_user_strategy(db_session)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={
            "legs": [{
                "label": "SELL X", "security_id": "1", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
                "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT", "product_type": "INTRADAY",
                "price": 50.0, "role": "primary",
            }],
            "entry_premium": 50.0,
        },
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 45.0}}}}}
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    assert close_user_strategy_now(db_session, user_strategy) is True

    db_session.refresh(run)
    assert run.manually_closed is True
    assert _today_run_count(user_strategy) == 1  # still burns the daily slot


def test_scheduled_close_open_run_does_not_mark_manual(db_session):
    user_strategy = _make_user_strategy(db_session)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={
            "legs": [{
                "label": "SELL X", "security_id": "1", "trading_symbol": "X", "exchange_segment": "NSE_FNO",
                "transaction_type": "SELL", "quantity": 75, "order_type": "LIMIT", "product_type": "INTRADAY",
                "price": 50.0, "role": "primary",
            }],
            "entry_premium": 50.0,
        },
    )
    db_session.add(run)
    db_session.commit()

    dhan = MagicMock()
    dhan.quote_data.return_value = {"status": "success", "data": {"status": "success", "data": {"NSE_FNO": {"1": {"last_price": 20.0}}}}}

    _close_open_run(db_session, dhan, user_strategy.user_id, run, is_live=False, reason="Exit conditions met.")

    db_session.refresh(run)
    assert run.manually_closed is False


# --- enter_user_strategy_now: the manual override ---


def test_enter_now_ignores_daily_cap_and_opens_a_position(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    _add_run(db_session, user_strategy, status="closed", manually_closed=True)  # today's slot already used
    assert _today_run_count(user_strategy) == 1

    fake_leg = OrderLeg(
        label="SELL X", security_id="1", trading_symbol="X", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )

    dhan = MagicMock()
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    captured_ctx = {}

    def fake_evaluate_entry(self, ctx):
        captured_ctx["today_run_count"] = ctx.today_run_count
        return [fake_leg]

    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", fake_evaluate_entry)
    monkeypatch.setattr("app.engine.runner._place_or_paper_leg", lambda *a, **k: MagicMock())

    entered = enter_user_strategy_now(db_session, user_strategy)

    assert entered is True
    assert captured_ctx["today_run_count"] == 0  # bypassed despite the real count being 1
    assert find_open_run(user_strategy) is not None


def test_enter_now_returns_false_when_conditions_not_met(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    dhan = MagicMock()
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))
    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", lambda self, ctx: None)

    entered = enter_user_strategy_now(db_session, user_strategy)

    assert entered is False
    assert find_open_run(user_strategy) is None


def test_enter_now_refuses_when_already_open(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    run = StrategyRun(
        user_strategy_id=user_strategy.id,
        started_at=datetime.now(timezone.utc),
        status="open",
        legs_planned={"legs": [], "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)

    try:
        enter_user_strategy_now(db_session, user_strategy)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "already has an open position" in str(exc)
