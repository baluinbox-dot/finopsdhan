"""Router/template smoke test for the 3-pair rolling strategy's admin +
configure wiring — registers the strategy, publishes it, and renders both
the list page and the configure-rolling page (the no-Dhan-connected
branch, which needs no mocking) to catch Jinja/route wiring mistakes."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import select

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


def test_banknifty_preview_defaults_to_a_100pt_gap_not_50(client, db_session, monkeypatch):
    """Regression test: BANKNIFTY/SENSEX trade in 100-point strikes, not
    50. Defaulting the preview (and the Strike Gap field) to 50 made the
    "nearest strike to ATM+50" search land exactly between two real
    100-point strikes, and the tie-break could put T on the *same* strike
    as M — a nonsensical window a real user actually hit."""
    _register_and_login(client, db_session, "trader2@example.com")
    user = db_session.scalar(select(User).where(User.email == "trader2@example.com"))
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()

    strategy = Strategy(
        name="Dynamic T-M-B 3-Pair Rolling Strategy", code_ref="three_pair_rolling", is_published=True,
    )
    db_session.add(strategy)
    db_session.commit()

    # BANKNIFTY strikes 100 apart; spot 57262.40 is nearest to 57300 (ATM).
    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": ["2026-08-27"]}}
    strikes = [57000, 57100, 57200, 57300, 57400, 57500, 57600]
    dhan.option_chain.return_value = {
        "status": "success",
        "data": {
            "status": "success",
            "data": {
                "last_price": 57262.40,
                "oc": {
                    f"{s}.000000": {
                        "ce": {"security_id": 10000 + s, "last_price": 60.0, "greeks": {}},
                        "pe": {"security_id": 20000 + s, "last_price": 55.0, "greeks": {}},
                    }
                    for s in strikes
                },
            },
        },
    }
    monkeypatch.setattr(
        "app.routers.strategies.get_user_dhan_client",
        lambda db, user: MagicMock(client=dhan),
    )

    resp = client.get(f"/strategies/{strategy.id}/configure-rolling?underlying=BANKNIFTY&expiry=2026-08-27")

    assert resp.status_code == 200
    assert "T <strong>57400</strong>" in resp.text
    assert "M (ATM) <strong>57300</strong>" in resp.text
    assert "B <strong>57200</strong>" in resp.text
    assert "100pt gap" in resp.text
    # The Strike Gap field itself must also default to 100, not 50 -- the
    # actual strategy config, not just the preview text, must be correct.
    assert 'name="strike_gap" value="100"' in resp.text


def test_switching_underlying_on_an_existing_instance_redefaults_the_gap(client, db_session, monkeypatch):
    """Regression test: a NIFTY instance saved before the 100pt-default fix
    (or just saved with the NIFTY default of 50) still has strike_gap: 50 in
    its persisted params. Reconfiguring that *same* instance and switching
    the Underlying dropdown to BANKNIFTY must NOT drag the old NIFTY gap
    along -- it must re-default to 100, otherwise the T==M bug reproduces
    on every existing instance regardless of the earlier fix."""
    _register_and_login(client, db_session, "trader3@example.com")
    user = db_session.scalar(select(User).where(User.email == "trader3@example.com"))
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()

    strategy = Strategy(
        name="Dynamic T-M-B 3-Pair Rolling Strategy", code_ref="three_pair_rolling", is_published=True,
    )
    db_session.add(strategy)
    db_session.commit()

    existing = UserStrategy(
        user_id=user.id,
        strategy_id=strategy.id,
        mode=StrategyMode.PAPER,
        is_active=True,
        label="3-Pair Rolling NIFTY",
        params={
            "underlying": "NIFTY",
            "expiry": "2026-08-25",
            "lots": 1,
            "start_time": "09:20",
            "end_time": "14:45",
            "strike_gap": 50.0,
            "daily_stop_loss": 10000.0,
            "daily_target": 15000.0,
        },
    )
    db_session.add(existing)
    db_session.commit()

    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": ["2026-08-27"]}}
    strikes = [57000, 57100, 57200, 57300, 57400, 57500, 57600]
    dhan.option_chain.return_value = {
        "status": "success",
        "data": {
            "status": "success",
            "data": {
                "last_price": 57262.40,
                "oc": {
                    f"{s}.000000": {
                        "ce": {"security_id": 10000 + s, "last_price": 60.0, "greeks": {}},
                        "pe": {"security_id": 20000 + s, "last_price": 55.0, "greeks": {}},
                    }
                    for s in strikes
                },
            },
        },
    }
    monkeypatch.setattr(
        "app.routers.strategies.get_user_dhan_client",
        lambda db, user: MagicMock(client=dhan),
    )

    resp = client.get(
        f"/strategies/{strategy.id}/configure-rolling"
        f"?user_strategy_id={existing.id}&underlying=BANKNIFTY&expiry=2026-08-27"
    )

    assert resp.status_code == 200
    assert "T <strong>57400</strong>" in resp.text
    assert "M (ATM) <strong>57300</strong>" in resp.text
    assert "B <strong>57200</strong>" in resp.text
    assert "100pt gap" in resp.text
    assert 'name="strike_gap" value="100"' in resp.text
