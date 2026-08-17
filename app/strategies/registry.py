"""Maps a Strategy row's `code_ref` to its implementation class.

To add a new strategy: write a class in this package implementing `Strategy`
(see base.py), then register it here and create/publish a `Strategy` DB row
whose `code_ref` matches the key used.
"""

from __future__ import annotations

from app.strategies.base import Strategy
from app.strategies.example_short_strangle import ExampleShortStrangle
from app.strategies.single_leg_seller_hedge import SingleLegSellerWithHedge

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "example_short_strangle": ExampleShortStrangle,
    "single_leg_seller_hedge": SingleLegSellerWithHedge,
}

# code_refs whose configuration is rich enough to need a dedicated
# configure page (GET/POST /strategies/{id}/configure) instead of the
# generic inline quick-enable card on the strategies list page.
RICH_CONFIG_STRATEGIES: set[str] = {"single_leg_seller_hedge"}


def get_strategy_class(code_ref: str) -> type[Strategy]:
    try:
        return STRATEGY_REGISTRY[code_ref]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy code_ref: {code_ref!r}") from exc
