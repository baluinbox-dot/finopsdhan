"""Coverage for the explicit Live-mode confirmation gate (2026-08-25):
requesting Live mode on any configure route must fall back to Paper unless
BOTH the user checked the confirmation checkbox on that exact submission
AND the server's own ALLOW_LIVE_TRADING switch is on — see
app.routers.strategies._resolve_requested_mode, shared by all 6 enable/
configure routes. Exercised here via the plain /enable route, the
simplest of the six; the logic itself is centralized in one function so
this is representative of all of them."""

from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.models import DhanCredential, Strategy, StrategyMode, User, UserStrategy

CAPTCHA_ANSWER = "8"  # conftest.client patches random.randint to always return 4


def _register_and_login(client, db_session, email: str, password: str = "supersecret1"):
    client.get("/auth/logout")
    client.get("/auth/register")
    client.post(
        "/auth/register",
        data={"email": email, "password": password, "confirm_password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    user = db_session.scalar(select(User).where(User.email == email))
    user.email_verified = True
    user.is_approved = True
    db_session.commit()

    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    return user


def _published_strategy(db_session) -> Strategy:
    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add(strategy)
    db_session.commit()
    return strategy


def _connect_dhan(db_session, user) -> None:
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()


def _enabled_mode(db_session, user, strategy) -> StrategyMode:
    us = db_session.scalar(
        select(UserStrategy).where(UserStrategy.user_id == user.id, UserStrategy.strategy_id == strategy.id)
    )
    return us.mode


def test_live_without_the_checkbox_stays_paper(client, db_session):
    user = _register_and_login(client, db_session, "trader1@example.com")
    _connect_dhan(db_session, user)
    strategy = _published_strategy(db_session)

    resp = client.post(f"/strategies/{strategy.id}/enable", data={"mode": "live"}, follow_redirects=False)

    assert resp.status_code == 303
    assert _enabled_mode(db_session, user, strategy) == StrategyMode.PAPER


def test_live_with_the_checkbox_but_server_switch_off_stays_paper(client, db_session):
    assert get_settings().allow_live_trading is False  # test default, sanity check
    user = _register_and_login(client, db_session, "trader2@example.com")
    _connect_dhan(db_session, user)
    strategy = _published_strategy(db_session)

    resp = client.post(
        f"/strategies/{strategy.id}/enable", data={"mode": "live", "live_confirmed": "true"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert _enabled_mode(db_session, user, strategy) == StrategyMode.PAPER


def test_live_with_the_checkbox_and_server_switch_on_actually_goes_live(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_live_trading", True)
    user = _register_and_login(client, db_session, "trader3@example.com")
    _connect_dhan(db_session, user)
    strategy = _published_strategy(db_session)

    resp = client.post(
        f"/strategies/{strategy.id}/enable", data={"mode": "live", "live_confirmed": "true"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert _enabled_mode(db_session, user, strategy) == StrategyMode.LIVE


def test_paper_mode_ignores_the_checkbox(client, db_session, monkeypatch):
    """The checkbox being checked must never itself force Live -- Mode
    still has to actually say "live" too."""
    monkeypatch.setattr(get_settings(), "allow_live_trading", True)
    user = _register_and_login(client, db_session, "trader4@example.com")
    _connect_dhan(db_session, user)
    strategy = _published_strategy(db_session)

    resp = client.post(
        f"/strategies/{strategy.id}/enable", data={"mode": "paper", "live_confirmed": "true"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert _enabled_mode(db_session, user, strategy) == StrategyMode.PAPER
