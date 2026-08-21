"""Runtime monkeypatch of the dhanhq SDK's HTTP layer to log the raw HTTP
status code (and any Retry-After header) on every failed call.

Why: `DhanHTTP._parse_response` (in the installed `dhanhq` package) discards
the HTTP status code once it's done building `remarks` — on a response
whose body doesn't carry Dhan's usual `errorCode`/`errorType`/`errorMessage`
keys (most commonly a 429 rate-limit response, but also e.g. an expired
token's 401, or a 500), `remarks` ends up
`{'error_code': None, 'error_type': None, 'error_message': None}` — which
looks *identical* regardless of which of those it actually was. That
ambiguity is exactly why the 2026-08-20 quote-outage incident's root cause
was never confirmed (it recovered before anyone could tell which one it
was). This patch changes nothing about what `_parse_response` returns —
it's purely additive, just logging the one piece of information the SDK
throws away, so the next occurrence is actually diagnosable.

Call `install()` once at app startup (see app/main.py's lifespan). Wrapped
in try/except so a future dhanhq release renaming `_parse_response` degrades
to "no extra logging" instead of crashing the app on startup.
"""

from __future__ import annotations

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
            return result

        DhanHTTP._parse_response = _parse_response_with_logging
        logger.info("Dhan HTTP diagnostics installed (status-code logging on failures)")
    except Exception:  # noqa: BLE001 — a diagnostic aid must never block app startup
        logger.exception("Could not install Dhan HTTP diagnostics — continuing without it")
