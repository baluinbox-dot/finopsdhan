"""run_user_strategy() emails the strategy owner when an evaluation pass
raises — e.g. the real "Invalid Expiry Date" Dhan error from 2026-08-26
that surfaced only as a log line nobody was watching. Reuses the
_make_user_strategy fixture pattern from test_daily_entry_cap.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.engine import runner
from app.engine.runner import run_user_strategy
from app.models import Strategy, StrategyMode, User, UserRole, UserStrategy


def _make_user_strategy(db_session, *, code_ref: str = "example_short_strangle") -> UserStrategy:
    user = User(email="trader@example.com", password_hash="x", role=UserRole.USER)
    strategy = Strategy(name="Test Strategy", code_ref=code_ref, is_published=True)
    db_session.add_all([user, strategy])
    db_session.flush()

    user_strategy = UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True, params={},
    )
    db_session.add(user_strategy)
    db_session.commit()
    return user_strategy


def _run_with_failing_entry(db_session, monkeypatch, user_strategy, *, exc: Exception, sent: list) -> None:
    dhan = MagicMock()
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    def _raise(self, ctx):
        raise exc

    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", _raise)
    monkeypatch.setattr(
        runner, "send_email",
        lambda to_email, subject, *, html_body, text_body: sent.append((to_email, subject, text_body)),
    )
    run_user_strategy(db_session, user_strategy)


def test_evaluation_error_sends_one_alert_email_to_the_strategy_owner(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    sent: list = []

    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("Dhan error: Invalid Expiry Date (HTTP 400)"), sent=sent)

    assert len(sent) == 1
    to_email, subject, body = sent[0]
    assert to_email == "trader@example.com"
    assert "Test Strategy" in subject
    assert "Invalid Expiry Date" in body


def test_repeated_errors_within_cooldown_only_send_one_email(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    sent: list = []

    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("boom"), sent=sent)
    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("boom again"), sent=sent)
    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("boom a third time"), sent=sent)

    assert len(sent) == 1  # cooldown suppresses the 2nd and 3rd


def test_a_new_email_goes_out_once_the_cooldown_has_elapsed(db_session, monkeypatch):
    user_strategy = _make_user_strategy(db_session)
    sent: list = []

    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("boom"), sent=sent)
    assert len(sent) == 1

    # Simulate the cooldown having fully elapsed without waiting for it.
    runner._last_error_alert_at[user_strategy.id] = datetime.now(timezone.utc) - runner._ERROR_ALERT_COOLDOWN - timedelta(seconds=1)

    _run_with_failing_entry(db_session, monkeypatch, user_strategy, exc=ValueError("boom again"), sent=sent)
    assert len(sent) == 2


def test_a_failed_email_send_never_propagates_out_of_run_user_strategy(db_session, monkeypatch):
    """send_email() itself already never raises in production, but the
    alert path around it must be just as defensive — a broken mail
    integration must never turn into a scheduler-crashing exception."""
    user_strategy = _make_user_strategy(db_session)
    dhan = MagicMock()
    monkeypatch.setattr(runner, "get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    def _raise_entry(self, ctx):
        raise ValueError("boom")

    monkeypatch.setattr("app.strategies.example_short_strangle.ExampleShortStrangle.evaluate_entry", _raise_entry)

    def _raise_email(*a, **k):
        raise RuntimeError("SMTP is on fire")

    monkeypatch.setattr(runner, "send_email", _raise_email)

    run_user_strategy(db_session, user_strategy)  # must not raise
