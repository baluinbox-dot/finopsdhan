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
    user.is_approved = True
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
    assert "No closed orders today." in resp.text
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


def test_dashboard_closed_orders_only_shows_today(client, db_session):
    """Closed Orders is a same-day quick-reference — a pair that closed on
    an earlier calendar day (IST) must not appear, even though it's still
    in the order history the Reports page would show."""
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 1)  # closes "now" == today

    strategy = Strategy(name="Yesterday Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(user_strategy_id=user_strategy.id, status="closed", legs_planned={})
    db_session.add(run)
    db_session.flush()
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add_all([
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="7000", trading_symbol="NIFTY 24700 CE",
            transaction_type="SELL", quantity=75, order_type="LIMIT", product_type="INTRADAY",
            price=120.0, status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=yesterday - timedelta(minutes=1),
        ),
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="7000", trading_symbol="NIFTY 24700 CE",
            transaction_type="BUY", quantity=75, order_type="LIMIT", product_type="INTRADAY",
            price=100.0, status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=yesterday,
        ),
    ])
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "NIFTY 24000 CE" in resp.text  # today's pair from _make_orders
    assert "NIFTY 24700 CE" not in resp.text  # yesterday's pair, filtered out
    assert "Showing 1 to 1 of 1 closed records" in resp.text


def test_dashboard_strategy_filter_narrows_running_and_closed(client, db_session):
    """The Strategy dropdown scopes both Running and Closed Orders to just
    that one UserStrategy instance's orders."""
    user = _register_and_login(client, db_session, "trader@example.com")
    _make_orders(db_session, user, 1)  # a closed pair under "Test Strategy"
    _make_running_orders(db_session, user, 1)  # a running leg under "Test Strategy 0"

    other_strategy = Strategy(name="Other Strategy", code_ref="x", is_published=True)
    db_session.add(other_strategy)
    db_session.flush()
    other_user_strategy = UserStrategy(user_id=user.id, strategy_id=other_strategy.id, mode=StrategyMode.PAPER)
    db_session.add(other_user_strategy)
    db_session.flush()

    # Filtering to the *other* strategy (no orders of its own) must hide
    # both the closed pair and the running leg from the other instances.
    resp = client.get(f"/dashboard?strategy_id={other_user_strategy.id}")
    assert resp.status_code == 200
    assert "NIFTY 24000 CE" not in resp.text  # the closed pair, filtered out
    assert "NIFTY 25000 PE" not in resp.text  # the running leg, filtered out
    assert "No running orders." in resp.text
    assert "No closed orders today." in resp.text

    # No filter (or filtering to its own strategy) still shows everything.
    resp_all = client.get("/dashboard")
    assert "NIFTY 24000 CE" in resp_all.text
    assert "NIFTY 25000 PE" in resp_all.text


def test_dashboard_hedge_and_primary_leg_sharing_a_contract_stay_separate(client, db_session):
    """Regression for a real incident (2026-08-25): a 3-Pair Rolling
    hedge and a separately rolled-in primary window leg landed on the
    same underlying option contract (same security_id). Both are
    genuinely still open -- their two *entry* orders must not get grouped
    together and wrongly paired off as a fabricated entry/exit "close"."""
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="3-Pair Rolling", code_ref="three_pair_rolling", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(user_strategy_id=user_strategy.id, status="open", legs_planned={})
    db_session.add(run)
    db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        # The hedge's real entry -- BUY 195, placed first.
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="61622",
            trading_symbol="NIFTY 24100 PE 2026-08-27", transaction_type="BUY", quantity=195,
            order_type="LIMIT", product_type="INTRADAY", price=12.80, role="hedge",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now - timedelta(minutes=30),
        ),
        # A completely unrelated primary leg's entry, rolled in later,
        # that happens to land on the exact same contract.
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="61622",
            trading_symbol="NIFTY 24100 PE 2026-08-27", transaction_type="SELL", quantity=65,
            order_type="LIMIT", product_type="INTRADAY", price=16.25, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now,
        ),
    ])
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    # Both legs still open -- neither the fabricated P&L (+672.75, from
    # (16.25-12.80)*195) nor "No running orders." must appear.
    assert "No running orders." not in resp.text
    assert "672.75" not in resp.text
    assert "Showing 1 to 2 of 2 running records" in resp.text
    assert "No closed orders today." in resp.text


def test_dashboard_leftover_order_in_a_closed_run_does_not_show_as_running(client, db_session):
    """Regression for a real incident (2026-08-28): a SENSEX Iron Condor
    run's PE leg pair picked up a duplicate closing order at its final
    close event, leaving that leg's order group with an odd count even
    though the run itself genuinely closed (StrategyRun.status ==
    "closed", with a real close timestamp). The dangling leftover order
    must not be shown as a still-open position days after the run ended."""
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="Iron Condor", code_ref="iron_condor_rolling", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(
        user_strategy_id=user_strategy.id, status="closed", legs_planned={},
        closed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(run)
    db_session.flush()

    now = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add_all([
        # Entry.
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="77000",
            trading_symbol="SENSEX 77000 PE 2026-08-27", transaction_type="BUY", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=66.00, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now,
        ),
        # Two "real" exit orders at the actual close event...
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="77000",
            trading_symbol="SENSEX 77000 PE 2026-08-27", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=17.40, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now + timedelta(hours=5),
        ),
        # ...plus a duplicate third order at the exact same close event --
        # the actual bug that leaves this group with an odd count (3).
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="77000",
            trading_symbol="SENSEX 77000 PE 2026-08-27", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=17.40, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now + timedelta(hours=5),
        ),
    ])
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No running orders." in resp.text  # the closed run's dangling leftover must not appear here


def test_dashboard_scaled_in_leg_still_open_shows_both_entries_as_running(client, db_session):
    """A leg that scaled in once (see app.engine.runner._apply_increments)
    -- original entry SELL 20, then a second same-direction SELL 20 add-on,
    neither closed yet -- must show as TWO running rows, not get
    mis-paired against each other as a fabricated close."""
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="Iron Condor", code_ref="iron_condor_rolling", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(user_strategy_id=user_strategy.id, status="open", legs_planned={})
    db_session.add(run)
    db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="76600",
            trading_symbol="SENSEX 76600 PE 2026-09-03", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=100.0, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now - timedelta(minutes=10),
        ),
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="76600",
            trading_symbol="SENSEX 76600 PE 2026-09-03", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=60.0, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now,
        ),
    ])
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No running orders." not in resp.text
    assert "Showing 1 to 2 of 2 running records" in resp.text
    assert "No closed orders today." in resp.text


def test_dashboard_scaled_in_leg_closed_by_one_order_shows_two_closed_pairs(client, db_session):
    """The same scaled-in leg, later closed by a single BUY covering the
    combined quantity (40) -- must produce two (entry, exit) pairs, each
    with its own correct entry price/quantity against the same exit price,
    not one fabricated pair or a leftover misread as still running."""
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="Iron Condor", code_ref="iron_condor_rolling", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    user_strategy = UserStrategy(user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER)
    db_session.add(user_strategy)
    db_session.flush()
    run = StrategyRun(user_strategy_id=user_strategy.id, status="closed", legs_planned={})
    db_session.add(run)
    db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="76600",
            trading_symbol="SENSEX 76600 PE 2026-09-03", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=100.0, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now - timedelta(minutes=10),
        ),
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="76600",
            trading_symbol="SENSEX 76600 PE 2026-09-03", transaction_type="SELL", quantity=20,
            order_type="LIMIT", product_type="MARGIN", price=60.0, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now - timedelta(minutes=5),
        ),
        Order(
            user_id=user.id, strategy_run_id=run.id, security_id="76600",
            trading_symbol="SENSEX 76600 PE 2026-09-03", transaction_type="BUY", quantity=40,
            order_type="LIMIT", product_type="MARGIN", price=30.0, role="primary",
            status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=now,
        ),
    ])
    db_session.commit()

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "No running orders." in resp.text
    assert "Showing 1 to 2 of 2 closed records" in resp.text
    # First entry: (100-30)*20 = 1400. Second (add-on) entry: (60-30)*20 = 600.
    assert "+1400.00" in resp.text
    assert "+600.00" in resp.text


def test_dashboard_underlying_filter_narrows_my_strategies_table(client, db_session):
    """The Underlying dropdown (NIFTY/BANKNIFTY/...) scopes the "My
    Strategies" table itself, not just Running/Closed Orders."""
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="Rolling Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    db_session.add(UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER,
        label="NIFTY Rolling", params={"underlying": "NIFTY"},
    ))
    db_session.add(UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER,
        label="BANKNIFTY Rolling", params={"underlying": "BANKNIFTY"},
    ))
    db_session.commit()

    resp = client.get("/dashboard?underlying=NIFTY")
    assert resp.status_code == 200
    assert "NIFTY Rolling" in resp.text
    assert "BANKNIFTY Rolling" not in resp.text

    resp_all = client.get("/dashboard")
    assert "NIFTY Rolling" in resp_all.text
    assert "BANKNIFTY Rolling" in resp_all.text


def test_dashboard_underlying_filter_narrows_running_and_closed_orders(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")

    nifty_strategy = Strategy(name="NIFTY Strategy", code_ref="x", is_published=True)
    db_session.add(nifty_strategy)
    db_session.flush()
    nifty_us = UserStrategy(
        user_id=user.id, strategy_id=nifty_strategy.id, mode=StrategyMode.PAPER,
        params={"underlying": "NIFTY"},
    )
    db_session.add(nifty_us)
    db_session.flush()
    nifty_run = StrategyRun(user_strategy_id=nifty_us.id, status="open", legs_planned={})
    db_session.add(nifty_run)
    db_session.flush()
    db_session.add(Order(
        user_id=user.id, strategy_run_id=nifty_run.id, security_id="1",
        trading_symbol="NIFTY 24000 CE", transaction_type="SELL", quantity=75,
        order_type="LIMIT", product_type="INTRADAY", price=100.0,
        status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=datetime.now(timezone.utc),
    ))

    sensex_strategy = Strategy(name="SENSEX Strategy", code_ref="x", is_published=True)
    db_session.add(sensex_strategy)
    db_session.flush()
    sensex_us = UserStrategy(
        user_id=user.id, strategy_id=sensex_strategy.id, mode=StrategyMode.PAPER,
        params={"underlying": "SENSEX"},
    )
    db_session.add(sensex_us)
    db_session.flush()
    sensex_run = StrategyRun(user_strategy_id=sensex_us.id, status="open", legs_planned={})
    db_session.add(sensex_run)
    db_session.flush()
    db_session.add(Order(
        user_id=user.id, strategy_run_id=sensex_run.id, security_id="2",
        trading_symbol="SENSEX 80000 PE", transaction_type="SELL", quantity=20,
        order_type="LIMIT", product_type="INTRADAY", price=200.0,
        status=OrderStatus.PAPER_FILLED, is_paper=True, placed_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    resp = client.get("/dashboard?underlying=SENSEX")
    assert resp.status_code == 200
    assert "SENSEX 80000 PE" in resp.text
    assert "NIFTY 24000 CE" not in resp.text


def test_dashboard_underlying_filter_resets_incompatible_strategy_filter(client, db_session):
    """If a specific strategy is selected and the user then switches the
    Underlying filter to something that strategy doesn't belong to, the
    stale strategy_id must be dropped rather than silently hiding
    everything (or crashing)."""
    user = _register_and_login(client, db_session, "trader@example.com")
    nifty_strategy = Strategy(name="NIFTY Strategy", code_ref="x", is_published=True)
    db_session.add(nifty_strategy)
    db_session.flush()
    nifty_us = UserStrategy(
        user_id=user.id, strategy_id=nifty_strategy.id, mode=StrategyMode.PAPER,
        label="NIFTY Instance", params={"underlying": "NIFTY"},
    )
    db_session.add(nifty_us)
    db_session.commit()

    # strategy_id points at the NIFTY instance, but underlying=BANKNIFTY
    # excludes it -- must not error, and the instance shouldn't show.
    resp = client.get(f"/dashboard?strategy_id={nifty_us.id}&underlying=BANKNIFTY")
    assert resp.status_code == 200
    assert "NIFTY Instance" not in resp.text


def test_dashboard_unknown_underlying_value_falls_back_to_all(client, db_session):
    user = _register_and_login(client, db_session, "trader@example.com")
    strategy = Strategy(name="Test Strategy", code_ref="x", is_published=True)
    db_session.add(strategy)
    db_session.flush()
    db_session.add(UserStrategy(
        user_id=user.id, strategy_id=strategy.id, mode=StrategyMode.PAPER,
        label="NIFTY Instance", params={"underlying": "NIFTY"},
    ))
    db_session.commit()

    resp = client.get("/dashboard?underlying=NOTREAL")
    assert resp.status_code == 200
    assert "NIFTY Instance" in resp.text  # falls back to "All Underlyings", not an empty/error page
