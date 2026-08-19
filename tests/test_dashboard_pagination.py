"""Router/template test for Recent Orders pagination on the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Order, OrderStatus, User

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


def _make_orders(db_session, user, count: int) -> None:
    now = datetime.now(timezone.utc)
    db_session.add_all([
        Order(
            user_id=user.id,
            security_id=str(1000 + i),
            trading_symbol=f"NIFTY {24000 + i * 50} CE",
            transaction_type="SELL",
            quantity=75,
            order_type="LIMIT",
            product_type="INTRADAY",
            price=100.0 + i,
            status=OrderStatus.PAPER_FILLED,
            is_paper=True,
            placed_at=now - timedelta(minutes=i),  # newest first == i=0
        )
        for i in range(count)
    ])
    db_session.commit()


def test_dashboard_defaults_to_10_per_page(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Showing 1 to 10 of 23 records" in resp.text
    assert "Page 1 of 3" in resp.text


def test_dashboard_respects_per_page_choice(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=5")
    assert resp.status_code == 200
    assert "Showing 1 to 5 of 23 records" in resp.text
    assert "Page 1 of 5" in resp.text


def test_dashboard_second_page_shows_next_slice(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=10&page=2")
    assert resp.status_code == 200
    assert "Showing 11 to 20 of 23 records" in resp.text
    assert "Page 2 of 3" in resp.text


def test_dashboard_last_page_shows_partial_range(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=10&page=3")
    assert resp.status_code == 200
    assert "Showing 21 to 23 of 23 records" in resp.text


def test_dashboard_invalid_per_page_falls_back_to_default(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 15)

    resp = client.get("/dashboard?per_page=999")
    assert resp.status_code == 200
    assert "Showing 1 to 10 of 15 records" in resp.text


def test_dashboard_page_beyond_range_clamps_to_last_page(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 12)

    resp = client.get("/dashboard?per_page=10&page=99")
    assert resp.status_code == 200
    assert "Showing 11 to 12 of 12 records" in resp.text
    assert "Page 2 of 2" in resp.text


def test_dashboard_no_orders_hides_pagination(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No orders yet." in resp.text
    assert "Showing" not in resp.text
