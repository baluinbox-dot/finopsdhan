"""Router/template smoke test for the P&L reports page — renders real
aggregated numbers from StrategyRun rows and catches Jinja/route wiring
mistakes that a pure runner-level test wouldn't."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Strategy, StrategyMode, StrategyRun, User, UserStrategy

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
    db_session.commit()

    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    return user


def test_reports_page_shows_aggregated_pnl(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")

    strategy = Strategy(name="Test Strategy", code_ref="example_short_strangle", is_published=True)
    db_session.add(strategy)
    db_session.flush()

    user_strategy = UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER, is_active=True, label="CE Seller",
    )
    db_session.add(user_strategy)
    db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        StrategyRun(
            user_strategy_id=user_strategy.id, started_at=now, status="closed",
            closed_at=now, realized_pnl=1500.0, evaluation_notes="Exit conditions met.",
            legs_planned={"legs": []},
        ),
        StrategyRun(
            user_strategy_id=user_strategy.id, started_at=now - timedelta(days=1), status="closed",
            closed_at=now - timedelta(days=1), realized_pnl=-500.0, evaluation_notes="Manually closed by user.",
            legs_planned={"legs": []},
        ),
        StrategyRun(  # still open — must not appear in the report at all
            user_strategy_id=user_strategy.id, started_at=now, status="open", legs_planned={"legs": []},
        ),
    ])
    db_session.commit()

    resp = client.get("/reports")
    assert resp.status_code == 200
    assert "+1,000" in resp.text or "+1000" in resp.text  # net total (1500 - 500)
    assert "CE Seller" in resp.text
    assert "2" in resp.text  # trade count somewhere on the page

    # Filtering by mode=live should exclude everything (all paper here).
    resp_live = client.get("/reports?mode=live")
    assert resp_live.status_code == 200
    assert "No closed trades match" in resp_live.text


def test_reports_page_empty_state(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert "No closed trades match" in resp.text


def test_reports_requires_login(client):
    resp = client.get("/reports", follow_redirects=False)
    assert resp.status_code == 401
