from __future__ import annotations

from app.backtest.costs import CostModel


def test_sell_fill_price_is_worse_than_quote():
    model = CostModel(slippage_bps=10.0)
    assert model.fill_price(100.0, "SELL") == 99.9  # 10bps = 0.1% of 100 = 0.10 -> 99.90


def test_buy_fill_price_is_worse_than_quote():
    model = CostModel(slippage_bps=10.0)
    assert model.fill_price(100.0, "BUY") == 100.1


def test_order_cost_includes_stt_only_on_sell():
    model = CostModel(brokerage_per_order=20.0, stt_sell_pct=0.1, other_charges_pct=0.05)
    sell_cost = model.order_cost(fill_price=100.0, quantity=75, transaction_type="SELL")
    buy_cost = model.order_cost(fill_price=100.0, quantity=75, transaction_type="BUY")
    premium_value = 100.0 * 75
    assert sell_cost == 20.0 + premium_value * 0.001 + premium_value * 0.0005
    assert buy_cost == 20.0 + premium_value * 0.0005  # no STT
    assert sell_cost > buy_cost


def test_order_cost_scales_with_quantity():
    model = CostModel()
    small = model.order_cost(fill_price=50.0, quantity=75, transaction_type="SELL")
    large = model.order_cost(fill_price=50.0, quantity=750, transaction_type="SELL")
    assert large > small
