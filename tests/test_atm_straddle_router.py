"""Router/template smoke test for the new strategy's admin + configure
wiring — registers the strategy, publishes it, and renders both the list
page and the configure-straddle page (the no-Dhan-connected branch, which
needs no mocking) to catch Jinja/route wiring mistakes."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Strategy, User

# conftest.client patches random.randint to always return 4, so every
# rendered CAPTCHA is "What is 4 + 4?" — this is the one correct answer.
_CAPTCHA_ANSWER = "8"


def _register_and_login(client, db_session, email: str, password: str = "supersecret1"):
    client.get("/auth/logout")  # in case an earlier user in this test is still logged in — GET /register redirects away otherwise
    client.get("/auth/register")
    client.post(
        "/auth/register",
        data={"email": email, "password": password, "confirm_password": password, "captcha_answer": _CAPTCHA_ANSWER},
        follow_redirects=False,
    )
    # Registration now requires clicking an emailed verification link before
    # login works — simulate that directly rather than needing real SMTP.
    user = db_session.scalar(select(User).where(User.email == email))
    user.email_verified = True
    user.is_approved = True
    db_session.commit()

    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "captcha_answer": _CAPTCHA_ANSWER},
        follow_redirects=False,
    )


def test_admin_can_publish_and_user_can_open_configure_page(client, db_session):
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env

    resp = client.post(
        "/strategies/admin/create",
        data={
            "name": "ATM Straddle Seller — Premium Trigger with Hedge",
            "description": "test",
            "code_ref": "atm_straddle_trigger_hedge",
            "default_params_json": "{}",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "atm_straddle_trigger_hedge"))
    assert strategy is not None
    assert strategy.is_published is False

    resp = client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(strategy)
    assert strategy.is_published is True

    # A regular user sees it on the list page, routed to the straddle configure URL.
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert f"/strategies/{strategy.id}/configure-straddle" in resp.text

    # Configure page renders without a Dhan connection (shows the connect-Dhan warning, doesn't 500).
    resp = client.get(f"/strategies/{strategy.id}/configure-straddle")
    assert resp.status_code == 200
    assert "Connect your Dhan account" in resp.text

    # Submitting without a Dhan connection is rejected, not a crash.
    resp = client.post(
        f"/strategies/{strategy.id}/configure-straddle",
        data={"underlying": "NIFTY", "expiry": "2026-08-27", "reference_premium": 100},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"
