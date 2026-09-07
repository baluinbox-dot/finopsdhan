"""_week_run_count: same idea as _today_run_count but counting since this
week's Monday 00:00 IST — used by a strategy that should stay flat for
the rest of the *week* after a stop, not just the rest of the day (see
app.strategies.rsi_call_writing, the first strategy to need this)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.engine.runner import _week_run_count, enter_user_strategy_now
from app.models import Strategy, StrategyMode, StrategyRun, User, UserRole, UserStrategy
from app.strategies.base import OrderLeg

IST = ZoneInfo("Asia/Kolkata")


def _make_user_strategy(db_session) -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True, params={})
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def _add_run_at(db_session, user_strategy: UserStrategy, started_at_utc: datetime) -> StrategyRun:
    run = StrategyRun(
        user_strategy_id=user_strategy.id, started_at=started_at_utc, status="closed",
        legs_planned={"legs": [], "entry_premium": 0},
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(user_strategy)
    return run


def test_run_earlier_this_week_counts(db_session):
    user_strategy = _make_user_strategy(db_session)
    now_ist = datetime.now(IST)
    monday = (now_ist - timedelta(days=now_ist.weekday())).replace(hour=9, minute=30, second=0, microsecond=0)
    _add_run_at(db_session, user_strategy, monday.astimezone(timezone.utc))

    assert _week_run_count(user_strategy) == 1


def test_run_from_last_week_does_not_count(db_session):
    user_strategy = _make_user_strategy(db_session)
    now_ist = datetime.now(IST)
    monday = (now_ist - timedelta(days=now_ist.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_week = monday - timedelta(seconds=1)  # one second before this week started
    _add_run_at(db_session, user_strategy, last_week.astimezone(timezone.utc))

    assert _week_run_count(user_strategy) == 0


def test_no_runs_at_all_counts_zero(db_session):
    user_strategy = _make_user_strategy(db_session)
    assert _week_run_count(user_strategy) == 0


def test_enter_now_bypasses_week_run_count(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    now_ist = datetime.now(IST)
    monday = (now_ist - timedelta(days=now_ist.weekday())).replace(hour=9, minute=30, second=0, microsecond=0)
    _add_run_at(db_session, user_strategy, monday.astimezone(timezone.utc))
    assert _week_run_count(user_strategy) == 1  # real count is non-zero

    fake_leg = OrderLeg(
        label="SELL X", security_id="1", trading_symbol="X", exchange_segment="NSE_FNO",
        transaction_type="SELL", quantity=75, price=50.0,
    )
    dhan = MagicMock()
    monkeypatch.setattr("app.engine.runner.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))
    monkeypatch.setattr("app.engine.runner._place_or_paper_leg", lambda *a, **k: MagicMock())

    captured = {}

    def fake_evaluate_entry(self, ctx):
        captured["week_run_count"] = ctx.week_run_count
        return [fake_leg]

    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", fake_evaluate_entry)

    entered = enter_user_strategy_now(db_session, user_strategy)

    assert entered is True
    assert captured["week_run_count"] == 0  # bypassed despite the real count being 1
