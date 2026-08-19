"""Router/template test for Closed and Running Orders pagination on the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Order, OrderStatus, Strategy, StrategyMode, StrategyRun, User, UserStrategy

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
    """Creates `count` closed entry+exit pairs (2*count Order rows) — the
    dashboard now only paginates closed pairs, not raw order rows, so each
    logical "record" the tests count needs both its SELL and its matching
    BUY. Each pair gets its own security_id under one shared run so they
    group independently (see app.routers.dashboard._pair_orders)."""
    strategy = Strategy(name="Test Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()  # assigns strategy.id before it's referenced below

    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()  # assigns user_strategy.id before the run references it

    run = StrategyRun(user_strategy_id=user_strategy.id, status="closed", legs_planned={})
    db_session.add(run)
    db_session.flush()  # assigns run.id before the orders below reference it

    now = datetime.now(timezone.utc)
    orders = []
    for i in range(count):
        sid = str(1000 + i)
        symbol = f"NIFTY {24000 + i * 50} CE"
        # i=0 is the most recent pair (matches the old "newest first == i=0" contract).
        entry_at = now - timedelta(minutes=i * 2 + 1)
        exit_at = now - timedelta(minutes=i * 2)
        orders.append(Order(
            user_id=user.id, strategy_run_id=run.id, security_id=sid, trading_symbol=symbol,
            transaction_type="SELL", quantity=75, order_type="LIMIT", product_type="INTRADAY",
            price=100.0 + i, status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=entry_at,
        ))
        orders.append(Order(
            user_id=user.id, strategy_run_id=run.id, security_id=sid, trading_symbol=symbol,
            transaction_type="BUY", quantity=75, order_type="LIMIT", product_type="INTRADAY",
            price=90.0 + i, status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=exit_at,
        ))
    db_session.add_all(orders)
    db_session.commit()


def test_dashboard_defaults_to_10_per_page(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Showing 1 to 10 of 23 closed records" in resp.text
    assert "Page 1 of 3" in resp.text


def test_dashboard_respects_per_page_choice(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=5")
    assert resp.status_code == 200
    assert "Showing 1 to 5 of 23 closed records" in resp.text
    assert "Page 1 of 5" in resp.text


def test_dashboard_second_page_shows_next_slice(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=10&page=2")
    assert resp.status_code == 200
    assert "Showing 11 to 20 of 23 closed records" in resp.text
    assert "Page 2 of 3" in resp.text


def test_dashboard_last_page_shows_partial_range(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 23)

    resp = client.get("/dashboard?per_page=10&page=3")
    assert resp.status_code == 200
    assert "Showing 21 to 23 of 23 closed records" in resp.text


def test_dashboard_invalid_per_page_falls_back_to_default(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 15)

    resp = client.get("/dashboard?per_page=999")
    assert resp.status_code == 200
    assert "Showing 1 to 10 of 15 closed records" in resp.text


def test_dashboard_page_beyond_range_clamps_to_last_page(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 12)

    resp = client.get("/dashboard?per_page=10&page=99")
    assert resp.status_code == 200
    assert "Showing 11 to 12 of 12 closed records" in resp.text
    assert "Page 2 of 2" in resp.text


def test_dashboard_no_orders_hides_pagination(client, db_session):
    _register_and_login(client, db_session, "trader@example.com")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No closed orders yet." in resp.text
    assert "Showing" not in resp.text


def test_dashboard_splits_running_and_closed_orders(client, db_session):
    """A leg with only an entry order (no exit yet) shows under Running
    Orders with its entry price and a live-priced placeholder; a leg with
    both an entry and an exit shows under Closed Orders instead, and never
    counts toward Running."""
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 1)  # one closed pair: NIFTY 24000 CE

    strategy = Strategy(name="Test Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(user_strategy_id=user_strategy.id, status="open", legs_planned={})
    db_session.add(run)
    db_session.flush()
    db_session.add(Order(
        user_id=user.id, strategy_run_id=run.id, security_id="9999", trading_symbol="NIFTY 24500 PE",
        transaction_type="SELL", quantity=75, order_type="LIMIT", product_type="INTRADAY",
        price=55.0, status=OrderStatus.PAPER_FILLED, is_paper=True,
        placed_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No running orders." not in resp.text
    assert "NIFTY 24500 PE" in resp.text  # the still-open leg
    assert "NIFTY 24000 CE" in resp.text  # the closed pair
    assert "Showing 1 to 1 of 1 closed records" in resp.text  # only the closed pair is counted


def _make_running_orders(db_session, user, count: int) -> None:
    """Creates `count` still-open legs (one order each, no exit) — each
    under its own run+security so none of them pair off with each other."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        strategy = Strategy(name=f"Test Strategy {i}", code_ref="x", is_published=True)
        db_session.add(strategy)
        db_session.flush()
        user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
        db_session.add(user_strategy)
        db_session.flush()
        run = StrategyRun(user_strategy_id=user_strategy.id, status="open", legs_planned={})
        db_session.add(run)
        db_session.flush()
        db_session.add(Order(
            user_id=user.id, strategy_run_id=run.id, security_id=str(2000 + i),
            trading_symbol=f"NIFTY {25000 + i * 50} PE",
            transaction_type="SELL", quantity=75, order_type="LIMIT", product_type="INTRADAY",
            price=50.0 + i, status=OrderStatus.PAPER_FILLED, is_paper=True,
            placed_at=now - timedelta(minutes=i),  # newest first == i=0
        ))
    db_session.commit()


def test_dashboard_paginates_running_orders_independently_of_closed(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_running_orders(db_session, user, 12)

    resp = client.get("/dashboard?running_per_page=10")
    assert resp.status_code == 200
    assert "Showing 1 to 10 of 12 running records" in resp.text
    assert "Page 1 of 2" in resp.text

    resp2 = client.get("/dashboard?running_per_page=10&running_page=2")
    assert resp2.status_code == 200
    assert "Showing 11 to 12 of 12 running records" in resp2.text


def test_dashboard_running_and_closed_pagination_are_independent(client, db_session):
    """Paging through Closed Orders must not disturb which page Running
    Orders is showing, and vice versa — they're two separate query params."""
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 15)  # 15 closed pairs
    _make_running_orders(db_session, user, 3)  # 3 running legs, well under one page

    resp = client.get("/dashboard?page=2&per_page=10")
    assert resp.status_code == 200
    assert "Showing 11 to 15 of 15 closed records" in resp.text
    assert "Showing 1 to 3 of 3 running records" in resp.text  # running still on its own page 1
