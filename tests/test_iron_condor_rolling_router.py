"""Router/template smoke test for the Iron Condor rolling strategy's admin +
configure wiring — registers the strategy, publishes it, and renders both
the list page and the configure-iron-condor page (the no-Dhan-connected
branch, which needs no mocking) to catch Jinja/route wiring mistakes. Also
covers the weekly/monthly expiry filter, which is unique to this strategy."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import select

from app.models import DhanCredential, Strategy, User

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


def test_admin_can_publish_and_user_can_open_configure_page(client, db_session):
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env

    resp = client.post(
        "/strategies/admin/create",
        data={
            "name": "Iron Condor — Untested-Side Rolling",
            "description": "test",
            "code_ref": "iron_condor_rolling",
            "default_params_json": "{}",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "iron_condor_rolling"))
    assert strategy is not None
    assert strategy.is_published is False

    resp = client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(strategy)
    assert strategy.is_published is True

    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert f"/strategies/{strategy.id}/configure-iron-condor" in resp.text

    resp = client.get(f"/strategies/{strategy.id}/configure-iron-condor")
    assert resp.status_code == 200
    assert "Connect your Dhan account" in resp.text

    resp = client.post(
        f"/strategies/{strategy.id}/configure-iron-condor",
        data={
            "underlying": "NIFTY", "expiry_type": "weekly", "expiry": "2026-08-27",
            "sell_offset_points": 250, "buy_offset_points": 350,
            "sl_target_mode": "fixed", "stop_loss_value": 10000, "target_value": 15000,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"


def test_expiry_type_filter_only_shows_the_selected_kind(client, db_session, monkeypatch):
    """Regression coverage for the weekly/monthly split: with mixed weekly
    and monthly dates in the raw expiry_list response, only the requested
    kind should populate the dropdown (the last date in each calendar month
    is "monthly", everything else in that month is "weekly")."""
    _register_and_login(client, db_session, "trader2@example.com")
    user = db_session.scalar(select(User).where(User.email == "trader2@example.com"))
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()

    strategy = Strategy(
        name="Iron Condor — Untested-Side Rolling", code_ref="iron_condor_rolling", is_published=True,
    )
    db_session.add(strategy)
    db_session.commit()

    dhan = MagicMock()
    # August 2026: three weekly expiries + the month's last one (monthly).
    dhan.expiry_list.return_value = {
        "status": "success",
        "data": {"status": "success", "data": ["2026-08-06", "2026-08-13", "2026-08-20", "2026-08-27"]},
    }
    monkeypatch.setattr(
        "app.routers.strategies.get_user_dhan_client",
        lambda db, user: MagicMock(client=dhan),
    )

    resp = client.get(f"/strategies/{strategy.id}/configure-iron-condor?underlying=NIFTY&expiry_type=monthly")
    assert resp.status_code == 200
    assert "2026-08-27" in resp.text
    assert "2026-08-06" not in resp.text
    assert "2026-08-13" not in resp.text
    assert "2026-08-20" not in resp.text

    resp = client.get(f"/strategies/{strategy.id}/configure-iron-condor?underlying=NIFTY&expiry_type=weekly")
    assert resp.status_code == 200
    assert "2026-08-06" in resp.text
    assert "2026-08-13" in resp.text
    assert "2026-08-20" in resp.text
    assert "2026-08-27" not in resp.text
