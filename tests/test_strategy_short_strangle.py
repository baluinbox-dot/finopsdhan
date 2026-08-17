from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.strategies.base import StrategyContext
from app.strategies.example_short_strangle import ExampleShortStrangle

# Dhan's real API double-wraps option-chain/expiry-list payloads: the SDK's
# own {"status", "data"} envelope contains *another* {"status", "data"}
# envelope inside it — verified live against the real API. Mocks match that
# actual shape, not a simplified one, so these tests don't silently drift
# from reality.
OPTION_CHAIN_RESPONSE = {
    "status": "success",
    "remarks": "",
    "data": {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                "23800.000000": {
                    "pe": {"security_id": 111, "last_price": 90.0, "greeks": {}},
                    "ce": {"security_id": 222, "last_price": 5.0, "greeks": {}},
                },
                "24200.000000": {
                    "pe": {"security_id": 333, "last_price": 4.0, "greeks": {}},
                    "ce": {"security_id": 444, "last_price": 95.0, "greeks": {}},
                },
            },
        },
    },
}


def _mock_dhan_client() -> MagicMock:
    dhan = MagicMock()
    dhan.expiry_list.return_value = {"status": "success", "data": {"status": "success", "data": ["2026-08-27"]}}
    dhan.option_chain.return_value = OPTION_CHAIN_RESPONSE
    return dhan


def test_evaluate_entry_sells_offset_strikes():
    dhan = _mock_dhan_client()
    ctx = StrategyContext(dhan_client=dhan, params={"strike_offset_points": 200, "lots": 1})
    strategy = ExampleShortStrangle()

    with patch("app.strategies.example_short_strangle.get_lot_size", return_value=75):
        legs = strategy.evaluate_entry(ctx)

    assert legs is not None
    assert len(legs) == 2
    assert {leg.transaction_type for leg in legs} == {"SELL"}
    assert {leg.security_id for leg in legs} == {"444", "111"}  # CE at 24200 (spot+200), PE at 23800 (spot-200)
    for leg in legs:
        assert leg.quantity == 75
        assert leg.order_type == "LIMIT"


def test_evaluate_entry_returns_none_when_no_expiry():
    dhan = _mock_dhan_client()
    dhan.expiry_list.return_value = {"status": "success", "data": []}
    ctx = StrategyContext(dhan_client=dhan, params={})
    strategy = ExampleShortStrangle()

    assert strategy.evaluate_entry(ctx) is None


def test_evaluate_exit_triggers_on_target():
    strategy = ExampleShortStrangle()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"111": {"last_price": 10.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"target_pct": 50, "stop_loss_pct": 30})

    open_run_notes = {
        "legs": [
            {"label": "SELL 23800 PE", "security_id": "111", "exchange_segment": "NSE_FNO"},
        ],
        "entry_premium": 90.0,
    }

    # premium collapsed from 90 -> 10, a ~89% drop, well past the 50% target
    assert strategy.evaluate_exit(ctx, open_run_notes) is True


def test_evaluate_exit_holds_when_within_bounds():
    strategy = ExampleShortStrangle()
    dhan = MagicMock()
    dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"111": {"last_price": 95.0}}}},
    }
    ctx = StrategyContext(dhan_client=dhan, params={"target_pct": 50, "stop_loss_pct": 30})

    open_run_notes = {
        "legs": [{"label": "SELL 23800 PE", "security_id": "111", "exchange_segment": "NSE_FNO"}],
        "entry_premium": 90.0,
    }

    # 90 -> 95 is only a ~5.6% move, inside both bounds
    assert strategy.evaluate_exit(ctx, open_run_notes) is False


def test_evaluate_exit_false_without_open_position():
    strategy = ExampleShortStrangle()
    ctx = StrategyContext(dhan_client=MagicMock(), params={})
    assert strategy.evaluate_exit(ctx, {}) is False
