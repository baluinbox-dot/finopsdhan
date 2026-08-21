"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.deps import CurrentUserOptional
from app.engine.scheduler import start_scheduler, stop_scheduler
from app.routers import admin, auth, dashboard, reports, settings as settings_router, strategies
from app.seed import seed_demo_strategy
from app.templating import static_url, url

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_demo_strategy()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="FinOps Algo", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    # Distinct name so this app's session cookie can't collide with a
    # sibling FinOps product's session cookie hosted on the same domain/IP
    # under a different path prefix.
    session_cookie="finopsdhan_session",
    same_site="lax",
    https_only=settings.session_cookie_https_only,
)

static_dir = Path(__file__).parent / "static"
app.mount(url("/static"), StaticFiles(directory=str(static_dir)), name="static")

# `prefix=settings.base_path` stacks with each router's own internal prefix
# (e.g. "/auth"), so routes end up registered at "{base_path}/auth/login"
# etc. Empty base_path (local dev) leaves routes exactly as before.
app.include_router(auth.router, prefix=settings.base_path)
app.include_router(settings_router.router, prefix=settings.base_path)
app.include_router(strategies.router, prefix=settings.base_path)
app.include_router(dashboard.router, prefix=settings.base_path)
app.include_router(reports.router, prefix=settings.base_path)
app.include_router(admin.router, prefix=settings.base_path)


@app.get(url("/"))
def index(current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse(url("/dashboard"), status_code=303)
    return RedirectResponse(url("/auth/login"), status_code=303)


@app.get(url("/healthz"))
def healthz():
    return {"status": "ok"}


# --- PWA: installable home-screen app (manifest + service worker) ---
#
# Both must go through `url()`/`static_url()` rather than being plain static
# files, because this app can be deployed under a path prefix (e.g.
# `BASE_PATH=/finopsdhan` on the VM, alongside other apps on the same
# domain) — a static manifest.json baked at repo-build time would have no
# way to know that prefix, and would send a mobile browser to the wrong
# start_url/scope on that deployment.
@app.get(url("/manifest.json"))
def pwa_manifest():
    manifest = {
        "name": "FinOps Algo",
        "short_name": "FinOps Algo",
        "description": "Multi-tenant algo-trading dashboard connected to your own Dhan broker account.",
        "start_url": url("/"),
        "scope": url("/"),
        "display": "standalone",
        "background_color": "#1e3a5f",
        "theme_color": "#1e3a5f",
        "orientation": "portrait-primary",
        "icons": [
            {"src": static_url("icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static_url("icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": static_url("icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static_url("icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return JSONResponse(manifest, media_type="application/manifest+json")


# Served from the app root (not under /static/) so its default registration
# scope covers the whole app — a service worker's scope defaults to the
# directory it's served from, and /static/sw.js would only ever be able to
# control pages under /static/, which is useless for installability.
@app.get(url("/sw.js"))
def pwa_service_worker():
    content = (static_dir / "sw.js").read_text(encoding="utf-8")
    return Response(content, media_type="application/javascript")
