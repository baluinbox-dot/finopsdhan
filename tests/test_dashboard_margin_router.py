"""Router-level coverage for the /dashboard/margin endpoint and the
Dashboard's new "Margin Used" column wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import select

from app.models import DhanCredential, Strategy, StrategyMode, StrategyRun, User, UserStrategy

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


def test_dashboard_page_shows_margin_used_column(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Margin Used" in resp.text


def test_margin_endpoint_returns_empty_without_dhan_connected(client, db_session):
    _register_and_login(client, db_session, "trader2@example.com")
    resp = client.get("/dashboard/margin")
    assert resp.status_code == 200
    assert resp.json() == {"positions": []}


def test_margin_endpoint_returns_combined_margin_for_an_open_position(client, db_session, monkeypatch):
    user = _register_and_login(client, db_session, "trader3@example.com")
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    strategy = Strategy(name="Test Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]
    db_session.add(StrategyRun(user_strategy_id=user_strategy.id, status="open", legs_planned={"legs": legs}))
    db_session.commit()

    dhan = MagicMock()
    dhan.dhan_http.client_id = "x"
    dhan.dhan_http.post.return_value = {"status": "success", "data": {"totalMargin": 12345.0}}
    monkeypatch.setattr("app.routers.dashboard.get_user_dhan_client", lambda db, user: MagicMock(client=dhan))

    resp = client.get("/dashboard/margin")
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions"] == [{"user_strategy_id": str(user_strategy.id), "margin_total": 12345.0}]
