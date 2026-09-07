"""Tests for the "Save & Backtest" button on a strategy's Configure page
(added alongside the ordinary Save button): submitting with action=backtest
must save/update the instance exactly as a normal submit would, but redirect
to that specific instance's backtest form instead of back to Strategies --
see app.routers.strategies' six configure-*-submit handlers and
app.routers.backtest._resolve_instance, which this exists to make
deterministic (no more picking an arbitrary instance via .first())."""

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
    return user


def _publish_strategy(client, db_session, code_ref: str, name: str) -> Strategy:
    _register_and_login(client, db_session, "baluinbox@gmail.com")  # SUPERADMIN_EMAIL from conftest env
    client.post(
        "/strategies/admin/create",
        data={"name": name, "description": "test", "code_ref": code_ref, "default_params_json": "{}"},
        follow_redirects=False,
    )
    strategy = db_session.scalar(select(Strategy).where(Strategy.code_ref == code_ref))
    client.post(f"/strategies/admin/{strategy.id}/toggle-publish", follow_redirects=False)
    db_session.refresh(strategy)
    return strategy


def _connect_dhan(db_session, user: User) -> None:
    db_session.add(DhanCredential(user_id=user.id, client_id="x", access_token_encrypted="y", is_active=True))
    db_session.commit()


def test_save_and_backtest_creates_a_new_instance_and_redirects_to_its_backtest_form(client, db_session):
    strategy = _publish_strategy(client, db_session, "rsi_call_writing", "RSI Call Writing — Weekly Roll")
    user = _register_and_login(client, db_session, "trader@example.com")
    _connect_dhan(db_session, user)

    resp = client.post(
        f"/strategies/{strategy.id}/configure-rsi-call-writing",
        data={
            "underlying": "NIFTY", "lots": 1, "entry_check_time": "15:25", "rsi_period": 3,
            "rsi_cross_level": 70, "strike_offset_pct": 1.0, "stop_loss_pct": 50,
            "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85, "order_type": "LIMIT",
            "mode": "paper", "action": "backtest",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    instance = db_session.scalar(
        select(UserStrategy).where(UserStrategy.user_id == user.id, UserStrategy.strategy_id == strategy.id)
    )
    assert instance is not None
    assert instance.params["underlying"] == "NIFTY"
    assert resp.headers["location"] == f"/backtest/{strategy.id}?user_strategy_id={instance.id}"


def test_plain_save_still_redirects_to_strategies(client, db_session):
    """Regression: omitting action (or action=save, the default) must keep
    the pre-existing behavior exactly -- only the new button changes anything."""
    strategy = _publish_strategy(client, db_session, "rsi_call_writing", "RSI Call Writing — Weekly Roll")
    user = _register_and_login(client, db_session, "trader2@example.com")
    _connect_dhan(db_session, user)

    resp = client.post(
        f"/strategies/{strategy.id}/configure-rsi-call-writing",
        data={
            "underlying": "NIFTY", "lots": 1, "entry_check_time": "15:25", "rsi_period": 3,
            "rsi_cross_level": 70, "strike_offset_pct": 1.0, "stop_loss_pct": 50,
            "profit_lock_trigger_pct": 35, "profit_lock_stop_pct": 85, "order_type": "LIMIT", "mode": "paper",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/strategies"


def test_save_and_backtest_updates_an_existing_instance_and_targets_it_specifically(client, db_session):
    """Reconfiguring (user_strategy_id passed) + Save & Backtest must update
    that SAME instance in place and redirect using its (unchanged) id --
    never create a second, ambiguous instance of the same strategy."""
    strategy = _publish_strategy(client, db_session, "iron_fly_adjustments", "Iron Fly with Adjustments")
    user = _register_and_login(client, db_session, "trader3@example.com")
    _connect_dhan(db_session, user)

    base_data = {
        "underlying": "NIFTY", "expiry_type": "weekly", "expiry": "2026-08-27",
        "ce_wing_offset_points": 300, "pe_wing_offset_points": 300,
        "sl_target_mode": "fixed", "stop_loss_value": 10000, "target_value": 15000, "mode": "paper",
    }
    client.post(f"/strategies/{strategy.id}/configure-iron-fly", data=base_data, follow_redirects=False)
    instance = db_session.scalar(
        select(UserStrategy).where(UserStrategy.user_id == user.id, UserStrategy.strategy_id == strategy.id)
    )
    assert instance is not None
    original_id = instance.id

    resp = client.post(
        f"/strategies/{strategy.id}/configure-iron-fly",
        data={**base_data, "user_strategy_id": str(original_id), "ce_wing_offset_points": 400, "action": "backtest"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/backtest/{strategy.id}?user_strategy_id={original_id}"

    db_session.refresh(instance)
    assert instance.id == original_id  # same row updated, not a second one created
    assert instance.params["ce_wing_offset_points"] == 400
    assert db_session.scalar(
        select(UserStrategy.id).where(UserStrategy.strategy_id == strategy.id)
    ) is not None
    all_instances = db_session.scalars(select(UserStrategy).where(UserStrategy.strategy_id == strategy.id)).all()
    assert len(all_instances) == 1
