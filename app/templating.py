"""Jinja2 template setup + a tiny session-backed flash-message helper."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

IST = ZoneInfo("Asia/Kolkata")


def url(path: str) -> str:
    """Prefix an absolute in-app path ("/dashboard", "/static/js/x.js", ...)
    with the configured BASE_PATH. Empty locally (no-op); e.g. "/finopsdhan"
    when hosted behind a reverse proxy under a path prefix. Every hardcoded
    absolute path in a redirect or template link must go through this —
    it's the one place that knows where this app is actually mounted."""
    base = get_settings().base_path.rstrip("/")
    return f"{base}{path}"


templates.env.globals["url"] = url

_static_version_cache: dict[str, str] = {}


def static_url(path: str) -> str:
    """URL for a /static asset (path relative to app/static, e.g.
    "js/live_pnl.js") with a cache-busting ?v=<mtime> query string.

    The StaticFiles mount serves these with Last-Modified/ETag but no
    explicit Cache-Control — with no Cache-Control, browsers apply their
    own heuristic freshness and can serve a JS/CSS file straight from disk
    cache on a plain reload without even asking the server, so a deploy
    that changes behavior (not just markup) can silently keep running old
    code after the page has visibly reloaded with new HTML. Confirmed live:
    a Dashboard reload after deploying the % Change column showed the new
    <th> and cell but the old live_pnl.js never populated it. The query
    string forces a new URL — and therefore an uncached fetch — every time
    the file's content actually changes; mtime is cached per path for this
    process's lifetime since the file won't change while it's running."""
    if path not in _static_version_cache:
        try:
            mtime = int((STATIC_DIR / path).stat().st_mtime)
        except OSError:
            mtime = 0
        _static_version_cache[path] = str(mtime)
    return f"{url('/static/' + path)}?v={_static_version_cache[path]}"


templates.env.globals["static_url"] = static_url


def to_ist(dt: datetime | None) -> datetime | None:
    """Convert a (usually UTC-stored) datetime to IST for display. Every
    timestamp in this app is stored in UTC (`datetime.now(timezone.utc)`) —
    templates must always go through this before formatting, or they'll
    silently show UTC labeled as if it were local trading-hours time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


templates.env.filters["to_ist"] = to_ist


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append({"message": message, "category": category})


def render(request: Request, name: str, context: dict[str, Any] | None = None, **status_kwargs):
    context = dict(context or {})
    context["flashes"] = request.session.pop("_flashes", [])
    context.setdefault("current_user", None)
    context.setdefault("base_path", get_settings().base_path)
    return templates.TemplateResponse(request, name, context, **status_kwargs)
