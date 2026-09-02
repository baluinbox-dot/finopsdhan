"""Coverage for the combined-margin display: app.dhan.helpers.fetch_combined_margin
(the raw Dhan /margincalculator/multi call, throttled per account) and
app.engine.pnl.compute_combined_margin (per-open-position wrapper, mirrors
compute_live_pnl's shape but can't batch across positions -- see its own
docstring for why)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.dhan import helpers as dhan_helpers
from app.dhan.helpers import fetch_combined_margin
from app.engine.pnl import compute_combined_margin
from app.models import Strategy, StrategyMode, StrategyRun, UserStrategy


@pytest.fixture(autouse=True)
def _clean_margin_registry():
    dhan_helpers._margin_state.clear()
    yield
    dhan_helpers._margin_state.clear()


def _client(client_id: str = "TESTCLIENT") -> MagicMock:
    client = MagicMock()
    client.dhan_http.client_id = client_id
    return client


# --- fetch_combined_margin ---


def test_fetch_combined_margin_returns_total_margin_on_success():
    client = _client()
    client.dhan_http.post.return_value = {
        "status": "success", "remarks": "",
        "data": {"totalMargin": 596239.2, "spanMargin": 386214.3, "exposure": 208292.4, "hedgeBenefit": 0.0},
    }
    legs = [
        {"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0},
        {"security_id": "2", "exchange_segment": "NSE_FNO", "transaction_type": "BUY", "quantity": 65, "product_type": "MARGIN", "price": 40.0},
    ]

    result = fetch_combined_margin(client, legs)

    assert result == 596239.2
    endpoint, payload = client.dhan_http.post.call_args[0]
    assert endpoint == "/margincalculator/multi"
    assert payload["includePosition"] is False
    assert payload["includeOrder"] is False
    assert len(payload["scripList"]) == 2
    assert payload["scripList"][0]["securityId"] == "1"
    assert payload["scripList"][0]["exchangeSegment"] == "NSE_FNO"
    assert payload["scripList"][0]["transactionType"] == "SELL"


def test_fetch_combined_margin_returns_none_for_empty_legs():
    client = _client()
    assert fetch_combined_margin(client, []) is None
    client.dhan_http.post.assert_not_called()


def test_fetch_combined_margin_returns_none_on_failure_status():
    client = _client()
    client.dhan_http.post.return_value = {"status": "failure", "remarks": {}}
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]

    assert fetch_combined_margin(client, legs) is None


def test_fetch_combined_margin_returns_none_when_call_raises():
    client = _client()
    client.dhan_http.post.side_effect = ConnectionError("boom")
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]

    assert fetch_combined_margin(client, legs) is None


def test_fetch_combined_margin_backs_off_this_accounts_state_on_failure():
    client = _client("BACKOFF_MARGIN")
    client.dhan_http.post.return_value = {"status": "failure", "remarks": {}}
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]

    fetch_combined_margin(client, legs)

    assert dhan_helpers._margin_state["BACKOFF_MARGIN"]["backoff"] == 2


# --- compute_combined_margin ---


def _make_open_position(legs: list[dict]) -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Test Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(id=uuid.uuid4(), user_id=uuid.uuid4(), strategy_id=strategy.id, params={}, mode=StrategyMode.PAPER, is_active=True)
    us.strategy = strategy
    run = StrategyRun(id=uuid.uuid4(), user_strategy_id=us.id, status="open", legs_planned={"legs": legs})
    us.runs = [run]
    return us


def _make_flat_strategy() -> UserStrategy:
    strategy = Strategy(id=uuid.uuid4(), name="Flat Strategy", code_ref="x", config_schema={}, default_params={})
    us = UserStrategy(id=uuid.uuid4(), user_id=uuid.uuid4(), strategy_id=strategy.id, params={}, mode=StrategyMode.PAPER, is_active=True)
    us.strategy = strategy
    us.runs = []
    return us


def test_compute_combined_margin_one_call_per_open_position():
    legs_a = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]
    legs_b = [{"security_id": "2", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 30, "product_type": "MARGIN", "price": 50.0}]
    us_a = _make_open_position(legs_a)
    us_b = _make_open_position(legs_b)

    dhan = _client()
    dhan.dhan_http.post.side_effect = [
        {"status": "success", "data": {"totalMargin": 111.0}},
        {"status": "success", "data": {"totalMargin": 222.0}},
    ]

    results = compute_combined_margin(dhan, [us_a, us_b])

    assert dhan.dhan_http.post.call_count == 2  # NOT batched -- each position's own scrip_list
    assert {r["user_strategy_id"]: r["margin_total"] for r in results} == {
        str(us_a.id): 111.0, str(us_b.id): 222.0,
    }


def test_compute_combined_margin_skips_flat_strategies():
    us = _make_flat_strategy()
    dhan = _client()

    results = compute_combined_margin(dhan, [us])

    assert results == []
    dhan.dhan_http.post.assert_not_called()


def test_compute_combined_margin_excludes_legs_already_closed_by_a_roll():
    legs = [
        {"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 131.60},
        {"security_id": "2", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 77.20},
    ]
    us = _make_open_position(legs)
    us.runs[0].legs_planned["leg_state"] = {"1": {"status": "closed"}}

    dhan = _client()
    dhan.dhan_http.post.return_value = {"status": "success", "data": {"totalMargin": 500.0}}

    results = compute_combined_margin(dhan, [us])

    assert len(results) == 1
    payload = dhan.dhan_http.post.call_args[0][1]
    assert [s["securityId"] for s in payload["scripList"]] == ["2"]  # only the still-open leg


def test_compute_combined_margin_does_not_double_send_a_revisited_security_id():
    """Regression for the bug found live 2026-09-02 on 4 real (paper-mode)
    positions -- a strike closed by an earlier roll and later reopened
    shares one security_id across two legs_planned["legs"] history
    entries; before the currently_open_legs dedup fix, both entries were
    sent to /margincalculator/multi, effectively double-billing that one
    physical leg's margin requirement. See tests/test_engine_rolls.py's
    engine-level regression test for the full writeup."""
    legs = [
        {"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0},  # stale, closed
        {"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 90.0},  # reopened
    ]
    us = _make_open_position(legs)
    us.runs[0].legs_planned["leg_state"] = {"1": {"status": "open"}}  # the reopened occurrence's true state

    dhan = _client()
    dhan.dhan_http.post.return_value = {"status": "success", "data": {"totalMargin": 500.0}}

    results = compute_combined_margin(dhan, [us])

    assert len(results) == 1
    payload = dhan.dhan_http.post.call_args[0][1]
    assert len(payload["scripList"]) == 1  # not 2 -- the stale entry must not be sent a second time
    assert payload["scripList"][0]["price"] == 90.0  # the reopened entry's own price, not the stale one's


def test_compute_combined_margin_none_when_call_fails():
    legs = [{"security_id": "1", "exchange_segment": "NSE_FNO", "transaction_type": "SELL", "quantity": 65, "product_type": "MARGIN", "price": 100.0}]
    us = _make_open_position(legs)

    dhan = _client()
    dhan.dhan_http.post.return_value = {"status": "failure", "remarks": {}}

    results = compute_combined_margin(dhan, [us])

    assert results == [{"user_strategy_id": str(us.id), "margin_total": None}]
