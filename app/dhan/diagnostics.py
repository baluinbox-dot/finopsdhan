"""Runtime monkeypatch of the dhanhq SDK's HTTP layer to log the raw HTTP
status code (and any Retry-After header) on every failed call, and to
recover the real Dhan error message the SDK itself throws away.

Why: `DhanHTTP._parse_response` (in the installed `dhanhq` package) discards
the HTTP status code once it's done building `remarks`, and only looks for
an error message under the response body's `errorCode`/`errorType`/
`errorMessage` keys. Several real Dhan failure bodies don't use that shape
at all — e.g. `{"data": {"811": "Invalid Expiry Date"}, "status": "failed"}`
(400) or `{"data": {"808": "Authentication Failed - ..."}, "status":
"failed"}` (401) — so `remarks` ends up
`{'error_code': None, 'error_type': None, 'error_message': None}`, which
looks *identical* to a true 429 rate-limit response that carries no body at
all. That ambiguity is exactly why the 2026-08-20 quote-outage incident's
root cause was never confirmed (it recovered before anyone could tell which
one it was), and why a real "Invalid Expiry Date" error on 2026-08-26 got
misreported as "you're probably being rate-limited" — see
app.dhan.helpers.format_dhan_error(), which is what actually turns this
into user/log-facing text and now reads the `status_code`/`raw_message`
this patch adds.

This patch changes nothing about what `_parse_response` returns on
success, and never removes/overwrites a `remarks` the SDK *did* manage to
parse — it only adds `status_code` and (when Dhan's body had a single
`{"data": {"<code>": "<message>"}}` entry) `raw_message` onto the ambiguous
empty-dict case, purely additive.

Call `install()` once at app startup (see app/main.py's lifespan). Wrapped
in try/except so a future dhanhq release renaming `_parse_response` degrades
to "no extra logging" instead of crashing the app on startup.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("app.dhan.http")

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    try:
        from dhanhq.dhan_http import DhanHTTP

        original = DhanHTTP._parse_response

        def _parse_response_with_logging(self, response):
            result = original(self, response)
            if result.get("status") != "success":
                try:
                    method = response.request.method if response.request is not None else "?"
                    url = response.request.url if response.request is not None else "?"
                    retry_after = response.headers.get("Retry-After")
                    body_preview = (response.text or "")[:300]
                    logger.warning(
                        "Dhan HTTP failure: %s %s -> status_code=%s retry_after=%s body=%r",
                        method, url, response.status_code, retry_after, body_preview,
                    )
                except Exception:  # noqa: BLE001 — logging must never break the real call
                    logger.exception("Failed to log Dhan HTTP failure diagnostics")

                remarks = result.get("remarks")
                is_ambiguous_empty = (
                    isinstance(remarks, dict)
                    and not remarks.get("error_code")
                    and not remarks.get("error_type")
                    and not remarks.get("error_message")
                )
                if is_ambiguous_empty:
                    raw_message = None
                    try:
                        body_json = json.loads(response.text or "")
                        data = body_json.get("data") if isinstance(body_json, dict) else None
                        if isinstance(data, dict) and len(data) == 1:
                            raw_message = next(iter(data.values()))
                    except Exception:  # noqa: BLE001 — best-effort only, never break the real call
                        pass
                    result["remarks"] = {
                        **remarks,
                        "status_code": getattr(response, "status_code", None),
                        "raw_message": raw_message,
                    }
            return result

        DhanHTTP._parse_response = _parse_response_with_logging
        logger.info("Dhan HTTP diagnostics installed (status-code logging on failures)")
    except Exception:  # noqa: BLE001 — a diagnostic aid must never block app startup
        logger.exception("Could not install Dhan HTTP diagnostics — continuing without it")
