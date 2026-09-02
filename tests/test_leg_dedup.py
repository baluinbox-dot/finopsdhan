"""Unit coverage for app.strategies.base's currently_open_legs /
dedupe_legs_by_security_id — the fix for a real bug found live 2026-09-02
(see tests/test_engine_rolls.py's engine-level regression test for the
full writeup): legs_planned["legs"] is append-only, so a strike a rolling
strategy closes and later reopens (spot oscillating back across a
boundary it already crossed) gets a *second* history entry sharing one
security_id, and leg_state only ever tracks that security_id's *latest*
status -- a naive "status != closed" filter over the raw list matches
BOTH entries once the strike comes back."""

from __future__ import annotations

from app.strategies.base import currently_open_legs, dedupe_legs_by_security_id


def _leg(sid: str, role: str = "primary", **overrides) -> dict:
    return {"security_id": sid, "role": role, "quantity": 75, "price": 100.0, "transaction_type": "SELL", **overrides}


def test_dedupe_keeps_the_last_occurrence_of_a_revisited_security_id():
    legs = [_leg("1", price=100.0), _leg("2", price=50.0), _leg("1", price=90.0)]  # "1" closed then reopened

    result = dedupe_legs_by_security_id(legs)

    by_sid = {leg["security_id"]: leg for leg in result}
    assert len(result) == 2
    assert by_sid["1"]["price"] == 90.0  # the later (reopened) entry, not the stale first one
    assert by_sid["2"]["price"] == 50.0


def test_dedupe_preserves_a_hedge_and_a_primary_sharing_one_security_id():
    """A hedge leg and an independently-managed primary leg can legitimately
    land on the exact same option contract (same security_id) while being
    two completely unrelated legs -- role is part of the identity, not
    just security_id (same reasoning as app.routers.dashboard._pair_orders's
    order-history grouping)."""
    legs = [_leg("1", role="primary"), _leg("1", role="hedge", transaction_type="BUY")]

    result = dedupe_legs_by_security_id(legs)

    assert len(result) == 2
    assert {leg["role"] for leg in result} == {"primary", "hedge"}


def test_currently_open_legs_excludes_a_revisited_security_ids_stale_closed_entry():
    legs = [_leg("1", price=100.0), _leg("2", price=50.0), _leg("1", price=90.0)]
    # leg_state only reflects the sid's latest state -- open, since the
    # second "1" entry (the reopen) is what's genuinely live right now.
    leg_state = {"1": {"status": "open"}, "2": {"status": "closed"}}

    result = currently_open_legs(legs, leg_state)

    assert len(result) == 1
    assert result[0]["security_id"] == "1"
    assert result[0]["price"] == 90.0  # the reopened entry, not the stale closed one


def test_currently_open_legs_is_a_no_op_when_no_security_id_repeats():
    """The overwhelming majority case -- must behave exactly like the
    naive filter it replaces when there's nothing to dedupe."""
    legs = [_leg("1"), _leg("2"), _leg("3")]
    leg_state = {"1": {"status": "open"}, "2": {"status": "closed"}, "3": {"status": "open"}}

    result = currently_open_legs(legs, leg_state)

    assert {leg["security_id"] for leg in result} == {"1", "3"}


def test_currently_open_legs_treats_a_sid_absent_from_leg_state_as_open():
    legs = [_leg("1")]
    assert currently_open_legs(legs, {}) == legs
