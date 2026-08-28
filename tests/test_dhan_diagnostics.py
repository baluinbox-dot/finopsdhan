"""app.dhan.diagnostics.install() patches a *third-party* class
(dhanhq.dhan_http.DhanHTTP) at runtime — these tests exist specifically to
catch it silently breaking the SDK's own parsing behavior, since nothing
else in the suite would notice that (everywhere else mocks the Dhan client
entirely, bypassing DhanHTTP altogether)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from app.dhan import diagnostics


def _fake_response(*, status_code: int, text: str = "", headers: dict | None = None):
    return SimpleNamespace(
        status_code=status_code,
        content=text.encode(),
        text=text,
        headers=headers or {},
        request=SimpleNamespace(method="GET", url="https://api.dhan.co/v2/marketfeed/quote"),
    )


def test_install_is_idempotent():
    diagnostics._installed = False
    diagnostics.install()
    diagnostics.install()  # must not double-wrap or raise
    diagnostics._installed = False  # reset so other test modules' first install() still runs for real


def test_patched_parse_response_returns_identical_result_on_success():
    from dhanhq.dhan_http import DhanHTTP

    diagnostics._installed = False
    diagnostics.install()

    http = DhanHTTP.__new__(DhanHTTP)  # skip __init__, _parse_response doesn't need instance state
    resp = _fake_response(status_code=200, text='{"status": "success", "data": {"x": 1}}')
    result = http._parse_response(resp)

    assert result["status"] == "success"
    assert result["data"] == {"status": "success", "data": {"x": 1}}


def test_patched_parse_response_logs_status_code_on_failure(caplog):
    from dhanhq.dhan_http import DhanHTTP

    diagnostics._installed = False
    diagnostics.install()

    http = DhanHTTP.__new__(DhanHTTP)
    resp = _fake_response(status_code=429, text="{}", headers={"Retry-After": "2"})

    with caplog.at_level(logging.WARNING, logger="app.dhan.http"):
        result = http._parse_response(resp)

    assert result["status"] == "failure"
    assert any("status_code=429" in r.message and "retry_after=2" in r.message for r in caplog.records)
    # A true 429 with an empty body carries no recoverable message — remarks
    # is enriched with status_code but raw_message stays None, so
    # format_dhan_error still falls back to its rate-limit guess for this
    # case specifically (see test_format_dhan_error.py).
    assert result["remarks"]["status_code"] == 429
    assert result["remarks"]["raw_message"] is None


def test_patched_parse_response_recovers_real_message_from_data_shaped_body():
    """Dhan sometimes fails with a body like
    {"data": {"811": "Invalid Expiry Date"}, "status": "failed"} — no
    errorCode/errorType/errorMessage keys, so the SDK's own parsing comes
    back fully empty. The patch should recover "Invalid Expiry Date" and
    the real HTTP status (400) onto `remarks` instead of leaving it
    indistinguishable from a true rate-limit response."""
    from dhanhq.dhan_http import DhanHTTP

    diagnostics._installed = False
    diagnostics.install()

    http = DhanHTTP.__new__(DhanHTTP)
    resp = _fake_response(
        status_code=400,
        text='{"data":{"811":"Invalid Expiry Date"},"status":"failed"}',
    )

    result = http._parse_response(resp)

    assert result["status"] == "failure"
    assert result["remarks"]["status_code"] == 400
    assert result["remarks"]["raw_message"] == "Invalid Expiry Date"
