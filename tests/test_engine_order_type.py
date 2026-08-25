"""Coverage for the app-wide order_type opt-in: resolve_order_type's
validation/default, and the engine's MARKET-price-zeroing at the point of
a *live* order placement (paper fills and the stored Order/OrderLeg price
must keep the real LTP snapshot regardless of order_type -- only the
actual live API call sends price=0 for a MARKET order)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from app.engine.runner import _place_or_paper_leg
from app.strategies.base import OrderLeg, resolve_order_type


def test_resolve_order_type_defaults_to_limit_when_missing():
    assert resolve_order_type({}) == "LIMIT"


def test_resolve_order_type_accepts_market():
    assert resolve_order_type({"order_type": "MARKET"}) == "MARKET"


def test_resolve_order_type_is_case_insensitive():
    assert resolve_order_type({"order_type": "market"}) == "MARKET"


def test_resolve_order_type_falls_back_to_limit_for_garbage():
    assert resolve_order_type({"order_type": "SL-M"}) == "LIMIT"


def _leg(order_type: str) -> OrderLeg:
    return OrderLeg(
        label="TEST", security_id="123", trading_symbol="NIFTY 24000 CE 2026-08-27",
        exchange_segment="NSE_FNO", transaction_type="SELL", quantity=75,
        order_type=order_type, product_type="INTRADAY", price=42.5,
    )


def test_paper_fill_keeps_the_real_price_regardless_of_order_type(db_session):
    dhan = MagicMock()
    order = _place_or_paper_leg(db_session, dhan, uuid.uuid4(), uuid.uuid4(), _leg("MARKET"), is_live=False)
    assert float(order.price) == 42.5
    assert order.order_type == "MARKET"
    dhan.place_order.assert_not_called()


def test_live_market_order_sends_zero_price_to_the_broker(db_session):
    dhan = MagicMock()
    dhan.place_order.return_value = {"status": "success", "data": {"orderId": "abc"}}
    order = _place_or_paper_leg(db_session, dhan, uuid.uuid4(), uuid.uuid4(), _leg("MARKET"), is_live=True)

    _, kwargs = dhan.place_order.call_args
    assert kwargs["order_type"] == "MARKET"
    assert kwargs["price"] == 0.0
    # The stored Order record still keeps the real LTP snapshot, not 0 --
    # only the outbound API call is affected.
    assert float(order.price) == 42.5


def test_live_limit_order_still_sends_the_real_price(db_session):
    dhan = MagicMock()
    dhan.place_order.return_value = {"status": "success", "data": {"orderId": "abc"}}
    order = _place_or_paper_leg(db_session, dhan, uuid.uuid4(), uuid.uuid4(), _leg("LIMIT"), is_live=True)

    _, kwargs = dhan.place_order.call_args
    assert kwargs["order_type"] == "LIMIT"
    assert kwargs["price"] == 42.5
    assert float(order.price) == 42.5
