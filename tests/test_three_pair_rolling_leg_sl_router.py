"""Router/template smoke test for the 3-Pair Rolling — Individual Leg SL &
Target strategy's admin + configure wiring — registers the strategy,
publishes it, and renders both the list page and the configure page to
catch Jinja/route wiring mistakes, plus a full save round-trip including
the leg SL/target validation."""

from __future__ import annotations

from sqlalchemy import select

from app.models import DhanCredential, Strategy, User, UserStrategy

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
            "name": "3-Pair Rolling — Individual Leg SL & Target",
            "description": "test",
            "code_ref": "three_pair_rolling_leg_sl_target",
            "default_params_json": "{}",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "three_pair_rolling_leg_sl_target"))
    assert strategy is not None
    assert strategy.is_published is False

    resp = client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(strategy)
    assert strategy.is_published is True

    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert f"/strategies/{strategy.id}/configure-rolling-legsl" in resp.text

    resp = client.get(f"/strategies/{strategy.id}/configure-rolling-legsl")
    assert resp.status_code == 200
    assert "Connect your Dhan account" in resp.text

    resp = client.post(
        f"/strategies/{strategy.id}/configure-rolling-legsl",
        data={
            "underlying": "NIFTY", "expiry": "2026-08-27", "strike_gap": 50,
            "leg_stop_loss_pct": 25, "leg_target_pct": 80,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"


def test_saved_instance_persists_the_chosen_leg_sl_and_target(client, db_session):
    _register_and_login(client, db_session, "baluinbox@gmail.com")
    client.post(
        "/strategies/admin/create",
        data={
            "name": "3-Pair Rolling — Individual Leg SL & Target",
            "description": "test",
            "code_ref": "three_pair_rolling_leg_sl_target",
            "default_params_json": "{}",
        },
    )
    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "three_pair_rolling_leg_sl_target"))
    client.post(f"/strategies/admin/{strategy.id}/toggle-publish")

    _register_and_login(client, db_session, "trader2@example.com")
    user = db_session.scalar(select(User).where(User.email == "trader2@example.com"))
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()

    client.post(
        f"/strategies/{strategy.id}/configure-rolling-legsl",
        data={
            "underlying": "NIFTY", "expiry": "2026-08-27", "strike_gap": 50,
            "leg_stop_loss_pct": 30, "leg_target_pct": 70, "lots": 2,
        },
    )

    inst = db_session.scalar(select(UserStrategy).where(UserStrategy.user_id == user.id))
    assert inst is not None
    assert inst.params["leg_stop_loss_pct"] == 30
    assert inst.params["leg_target_pct"] == 70
    assert inst.params["lots"] == 2
    assert inst.label == "3-Pair Rolling Leg-SL NIFTY"


def test_invalid_leg_stop_loss_pct_is_rejected(client, db_session):
    _register_and_login(client, db_session, "baluinbox@gmail.com")
    client.post(
        "/strategies/admin/create",
        data={
            "name": "3-Pair Rolling — Individual Leg SL & Target",
            "description": "test",
            "code_ref": "three_pair_rolling_leg_sl_target",
            "default_params_json": "{}",
        },
    )
    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == "three_pair_rolling_leg_sl_target"))
    client.post(f"/strategies/admin/{strategy.id}/toggle-publish")

    _register_and_login(client, db_session, "trader3@example.com")
    user = db_session.scalar(select(User).where(User.email == "trader3@example.com"))
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()

    resp = client.post(
        f"/strategies/{strategy.id}/configure-rolling-legsl",
        data={
            "underlying": "NIFTY", "expiry": "2026-08-27", "strike_gap": 50,
            "leg_stop_loss_pct": 999, "leg_target_pct": 80,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Leg Stop Loss must be 25% or 30%" in resp.text
    assert db_session.scalar(select(UserStrategy).where(UserStrategy.user_id == user.id)) is None
