"""Router/template smoke test for the 3-pair rolling strategy's admin +
configure wiring — registers the strategy, publishes it, and renders both
the list page and the configure-rolling page (the no-Dhan-connected
branch, which needs no mocking) to catch Jinja/route wiring mistakes."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Strategy, User

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


def test_admin_can_publish_and_user_can_open_configure_page(client, db_session):
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env

    resp = client.post(
        "/strategies/admin/create",
        data={
            "name": "Dynamic T-M-B 3-Pair Rolling Strategy",
            "description": "test",
            "code_ref": "three_pair_rolling",
            "default_params_json": "{}",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "three_pair_rolling"))
    assert strategy is not None
    assert strategy.is_published is False

    resp = client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(strategy)
    assert strategy.is_published is True

    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert f"/strategies/{strategy.id}/configure-rolling" in resp.text

    resp = client.get(f"/strategies/{strategy.id}/configure-rolling")
    assert resp.status_code == 200
    assert "Connect your Dhan account" in resp.text

    resp = client.post(
        f"/strategies/{strategy.id}/configure-rolling",
        data={"underlying": "NIFTY", "expiry": "2026-08-27", "strike_gap": 50},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"
