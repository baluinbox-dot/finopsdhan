"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.deps import CurrentUserOptional
from app.engine.scheduler import start_scheduler, stop_scheduler
from app.routers import auth, dashboard, settings as settings_router, strategies
from app.seed import seed_demo_strategy
from app.templating import url

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_demo_strategy()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="FinOps Dhan Algo", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    # Distinct name so this app's session cookie can't collide with a
    # sibling FinOps product's session cookie hosted on the same domain/IP
    # under a different path prefix.
    session_cookie="finopsdhan_session",
    same_site="lax",
    https_only=settings.is_production,
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


@app.get(url("/"))
def index(current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse(url("/dashboard"), status_code=303)
    return RedirectResponse(url("/auth/login"), status_code=303)


@app.get(url("/healthz"))
def healthz():
    return {"status": "ok"}
