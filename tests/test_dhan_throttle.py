"""Coverage for the per-Dhan-account throttle/backoff in app/dhan/helpers.py
(2026-08-25 fix): two different accounts must never share pacing state, and
a real failure must widen an account's own spacing until it succeeds again,
rather than hammering at a fixed pace regardless of what just happened."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.dhan import helpers as dhan_helpers


@pytest.fixture(autouse=True)
def _clean_throttle_registries():
    """Throttle state is module-level (keyed by client_id, shared across
    the whole process by design) -- reset it around every test so one
    test's backoff/timing can never leak into another's."""
    dhan_helpers._option_chain_state.clear()
    dhan_helpers._quote_state.clear()
    yield
    dhan_helpers._option_chain_state.clear()
    dhan_helpers._quote_state.clear()


def _client(client_id: str) -> MagicMock:
    client = MagicMock()
    client.dhan_http.client_id = client_id
    return client


def test_client_key_reads_the_accounts_own_client_id():
    assert dhan_helpers._client_key(_client("ABC123")) == "ABC123"


def test_client_key_falls_back_to_a_shared_bucket_when_missing():
    bare = MagicMock(spec=[])  # no dhan_http attribute at all
    assert dhan_helpers._client_key(bare) == "_unknown_"


def test_two_different_accounts_get_independent_throttle_state():
    client_a = _client("ACCOUNT_A")
    client_b = _client("ACCOUNT_B")

    dhan_helpers._throttle_quote(client_a)
    dhan_helpers._throttle_quote(client_b)

    assert set(dhan_helpers._quote_state.keys()) == {"ACCOUNT_A", "ACCOUNT_B"}
    # Each account's own state object, not the same one shared -- a failure
    # or wait on one must never be visible to the other.
    assert dhan_helpers._quote_state["ACCOUNT_A"] is not dhan_helpers._quote_state["ACCOUNT_B"]


def test_a_failure_backs_off_and_a_success_resets_it():
    client = _client("BACKOFF_TEST")
    state = dhan_helpers._throttle_quote(client)
    assert state["backoff"] == 1

    dhan_helpers._note_quote_result(state, ok=False)
    assert state["backoff"] == 2
    dhan_helpers._note_quote_result(state, ok=False)
    assert state["backoff"] == 4

    dhan_helpers._note_quote_result(state, ok=True)
    assert state["backoff"] == 1  # a real success clears the backoff immediately


def test_backoff_is_capped_and_never_grows_unbounded():
    client = _client("CAP_TEST")
    state = dhan_helpers._throttle_quote(client)
    for _ in range(10):
        dhan_helpers._note_quote_result(state, ok=False)
    assert state["backoff"] == dhan_helpers._BACKOFF_CAP_MULTIPLIER


def test_option_chain_and_quote_families_track_backoff_independently():
    client = _client("SEPARATE_FAMILIES")
    quote_state = dhan_helpers._throttle_quote(client)
    chain_state = dhan_helpers._throttle_option_chain(client)

    dhan_helpers._note_quote_result(quote_state, ok=False)
    assert quote_state["backoff"] == 2
    assert chain_state["backoff"] == 1  # untouched by the quote family's failure


def test_fetch_quotes_backs_off_this_accounts_state_on_a_failed_call():
    client = _client("FETCH_QUOTES_FAIL")
    client.quote_data.return_value = {"status": "failure", "remarks": {"error_code": None, "error_type": None, "error_message": None}}

    result = dhan_helpers.fetch_quotes(client, {"NSE_FNO": [123]})

    assert result == {}
    assert dhan_helpers._quote_state["FETCH_QUOTES_FAIL"]["backoff"] == 2


def test_fetch_quotes_resets_backoff_on_a_successful_call_after_a_failure(monkeypatch):
    # Two calls on the same account back-to-back would otherwise really
    # sleep out the backed-off interval between them -- irrelevant to what
    # this test checks (the backoff bookkeeping itself), so zero it out.
    monkeypatch.setattr(dhan_helpers, "_QUOTE_MIN_INTERVAL_SECONDS", 0)
    client = _client("FETCH_QUOTES_RECOVER")
    client.quote_data.return_value = {"status": "failure", "remarks": {}}
    dhan_helpers.fetch_quotes(client, {"NSE_FNO": [123]})
    assert dhan_helpers._quote_state["FETCH_QUOTES_RECOVER"]["backoff"] == 2

    client.quote_data.return_value = {
        "status": "success",
        "data": {"status": "success", "data": {"NSE_FNO": {"123": {"last_price": 100.0}}}},
    }
    result = dhan_helpers.fetch_quotes(client, {"NSE_FNO": [123]})

    assert result == {("NSE_FNO", "123"): {"last_price": 100.0}}
    assert dhan_helpers._quote_state["FETCH_QUOTES_RECOVER"]["backoff"] == 1
