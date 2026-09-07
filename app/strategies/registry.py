"""Maps a Strategy row's `code_ref` to its implementation class.

To add a new strategy: write a class in this package implementing `Strategy`
(see base.py), then register it here and create/publish a `Strategy` DB row
whose `code_ref` matches the key used.
"""

from __future__ import annotations

from app.strategies.atm_straddle_trigger_hedge import ATMStraddleTriggerHedge
from app.strategies.base import Strategy
from app.strategies.dynamic_strangle import DynamicStrangleStrategy
from app.strategies.example_short_strangle import ExampleShortStrangle
from app.strategies.iron_condor_rolling import IronCondorRollingStrategy
from app.strategies.iron_fly_adjustments import IronFlyAdjustmentsStrategy
from app.strategies.rsi_call_writing import RSICallWritingStrategy
from app.strategies.single_leg_seller_hedge import SingleLegSellerWithHedge
from app.strategies.three_pair_rolling import ThreePairRollingStrategy
from app.strategies.three_pair_rolling_leg_sl_target import ThreePairRollingLegSLTargetStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "example_short_strangle": ExampleShortStrangle,
    "single_leg_seller_hedge": SingleLegSellerWithHedge,
    "atm_straddle_trigger_hedge": ATMStraddleTriggerHedge,
    "three_pair_rolling": ThreePairRollingStrategy,
    "three_pair_rolling_leg_sl_target": ThreePairRollingLegSLTargetStrategy,
    "iron_condor_rolling": IronCondorRollingStrategy,
    "iron_fly_adjustments": IronFlyAdjustmentsStrategy,
    "dynamic_strangle": DynamicStrangleStrategy,
    "rsi_call_writing": RSICallWritingStrategy,
}

# code_refs whose configuration is rich enough to need a dedicated
# configure page instead of the generic inline quick-enable card on the
# strategies list page. Each one's configure page lives at its own route —
# see the configure_action lookup in app/templates/strategies/list.html.
RICH_CONFIG_STRATEGIES: set[str] = {
    "single_leg_seller_hedge",
    "atm_straddle_trigger_hedge",
    "three_pair_rolling",
    "three_pair_rolling_leg_sl_target",
    "iron_condor_rolling",
    "iron_fly_adjustments",
    "dynamic_strangle",
    "rsi_call_writing",
}


# code_refs the in-app Backtest feature (Phase C) will actually run --
# proven end-to-end against real downloaded data during the backtest
# effort, not just theoretically compatible with run_backtest's generic
# hasattr-based patching. Deliberately excludes:
#   - atm_straddle_trigger_hedge: flagged unsuitable during Phase B
#     validation (its own RSI-on-live-quotes design doesn't reconstruct
#     faithfully from the downloaded offset-band data).
#   - iron_condor_rolling: shares the same auto_advance_expiry mechanism
#     as iron_fly_adjustments (already wired into the engine) but hasn't
#     itself been run against real data yet -- Balu's own call to focus on
#     RSI Call Writing first.
#   - example_short_strangle: the seeded demo strategy, not a real one.
# Add a code_ref here only after it's actually been validated the same
# way every strategy above it was (real local data, sane trade count, no
# stuck/runaway trades) -- see app/backtest/engine.py's module docstring
# history for that bar.
BACKTEST_READY_STRATEGIES: set[str] = {
    "three_pair_rolling",
    "three_pair_rolling_leg_sl_target",
    "single_leg_seller_hedge",
    "iron_fly_adjustments",
    "dynamic_strangle",
    "rsi_call_writing",
}


# code_refs that hold against a fixed, configured expiry and never
# resolve one dynamically themselves (Iron Condor Rolling, Iron Fly
# Adjustments) -- these need run_backtest's auto_advance_expiry=True, or
# a backtest spanning more than one expiry cycle stalls forever the
# moment the configured expiry passes (see app.backtest.engine's own
# auto_advance_expiry docstring). Every other strategy resolves its own
# expiry each cycle (list_expiries + its own picking logic, or doesn't
# care about a real expiry date at all) and must leave this off.
AUTO_ADVANCE_EXPIRY_STRATEGIES: set[str] = {
    "iron_condor_rolling",
    "iron_fly_adjustments",
}


def get_strategy_class(code_ref: str) -> type[Strategy]:
    try:
        return STRATEGY_REGISTRY[code_ref]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy code_ref: {code_ref!r}") from exc
