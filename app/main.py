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
    same_site="lax",
    https_only=settings.is_production,
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(strategies.router)
app.include_router(dashboard.router)


@app.get("/")
def index(current_user: CurrentUserOptional):
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/auth/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
