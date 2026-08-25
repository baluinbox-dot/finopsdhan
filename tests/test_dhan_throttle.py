"""Coverage for the per-Dhan-account throttle/backoff in app/dhan/helpers.py
(2026-08-25 fix): two different accounts must never share pacing state, and
a real failure must widen an account's own spacing until it succeeds
again, rather than hammering at a fixed pace regardless of what just
happened. `_throttled_call` serializes the wait, the call itself, and the
resulting backoff update all under one lock -- see its own docstring for
why (a real race in an earlier version of this fix)."""

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


def _ok_response() -> dict:
    return {"status": "success", "data": {}}


def _fail_response() -> dict:
    return {"status": "failure", "remarks": {}}


def test_client_key_reads_the_accounts_own_client_id():
    assert dhan_helpers._client_key(_client("ABC123")) == "ABC123"


def test_client_key_falls_back_to_a_shared_bucket_when_missing():
    bare = MagicMock(spec=[])  # no dhan_http attribute at all
    assert dhan_helpers._client_key(bare) == "_unknown_"


def test_two_different_accounts_get_independent_throttle_state():
    client_a = _client("ACCOUNT_A")
    client_b = _client("ACCOUNT_B")

    dhan_helpers._throttled_call(dhan_helpers._quote_state, client_a, 0, _ok_response)
    dhan_helpers._throttled_call(dhan_helpers._quote_state, client_b, 0, _ok_response)

    assert set(dhan_helpers._quote_state.keys()) == {"ACCOUNT_A", "ACCOUNT_B"}
    # Each account's own state object, not the same one shared -- a
    # failure or backoff on one must never be visible to the other.
    assert dhan_helpers._quote_state["ACCOUNT_A"] is not dhan_helpers._quote_state["ACCOUNT_B"]


def test_a_failure_backs_off_and_a_success_resets_it():
    client = _client("BACKOFF_TEST")

    dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _fail_response)
    assert dhan_helpers._quote_state["BACKOFF_TEST"]["backoff"] == 2

    dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _fail_response)
    assert dhan_helpers._quote_state["BACKOFF_TEST"]["backoff"] == 4

    dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _ok_response)
    assert dhan_helpers._quote_state["BACKOFF_TEST"]["backoff"] == 1  # a real success clears it immediately


def test_an_exception_also_backs_off_and_reraises():
    client = _client("EXCEPTION_TEST")

    def _raise():
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _raise)

    assert dhan_helpers._quote_state["EXCEPTION_TEST"]["backoff"] == 2


def test_backoff_is_capped_and_never_grows_unbounded():
    client = _client("CAP_TEST")
    for _ in range(10):
        dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _fail_response)
    assert dhan_helpers._quote_state["CAP_TEST"]["backoff"] == dhan_helpers._BACKOFF_CAP_MULTIPLIER


def test_option_chain_and_quote_families_track_backoff_independently():
    client = _client("SEPARATE_FAMILIES")

    dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _fail_response)
    dhan_helpers._throttled_call(dhan_helpers._option_chain_state, client, 0, _ok_response)

    assert dhan_helpers._quote_state["SEPARATE_FAMILIES"]["backoff"] == 2
    assert dhan_helpers._option_chain_state["SEPARATE_FAMILIES"]["backoff"] == 1  # untouched by the quote family's failure


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


def test_concurrent_calls_for_the_same_account_are_fully_serialized():
    """Regression for a real race in an earlier version of this fix: the
    wait-then-call-then-record steps must all happen under one lock, so a
    second thread can never start its own call until the first thread's
    full call (including its outcome being recorded) has finished."""
    import threading
    import time as time_module

    client = _client("RACE_TEST")
    events: list[str] = []
    first_call_started = threading.Event()
    let_first_call_finish = threading.Event()

    def _first_call():
        events.append("first-start")
        first_call_started.set()
        let_first_call_finish.wait(timeout=2)
        events.append("first-end")
        return _fail_response()

    def _second_call():
        events.append("second-start")
        return _ok_response()

    t1 = threading.Thread(target=lambda: dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _first_call))
    t1.start()
    assert first_call_started.wait(timeout=2)  # t1 is inside its call (holding the lock) before t2 starts

    t2 = threading.Thread(target=lambda: dhan_helpers._throttled_call(dhan_helpers._quote_state, client, 0, _second_call))
    t2.start()
    time_module.sleep(0.05)  # give t2 a chance to (wrongly) start early if the lock didn't really cover fn()
    assert "second-start" not in events  # t2 must still be blocked, waiting on t1's lock

    let_first_call_finish.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert events == ["first-start", "first-end", "second-start"]
