"""Background polling loop: every `STRATEGY_POLL_INTERVAL_SECONDS`, evaluate
every active UserStrategy once. One shared APScheduler instance for the
whole process — started/stopped from app/main.py's lifespan handler.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.engine.runner import run_user_strategy
from app.models import User, UserStrategy

logger = logging.getLogger("app.engine.scheduler")

_scheduler: BackgroundScheduler | None = None


def _tick() -> None:
    db = SessionLocal()
    try:
        active = db.scalars(
            select(UserStrategy)
            .where(UserStrategy.is_active == True)  # noqa: E712
            .options(
                selectinload(UserStrategy.user).selectinload(User.dhan_credential),
                selectinload(UserStrategy.strategy),
                selectinload(UserStrategy.runs),
            )
        ).all()
        for user_strategy in active:
            run_user_strategy(db, user_strategy)
    except Exception:
        logger.exception("Scheduler tick failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    _scheduler.add_job(
        _tick,
        "interval",
        seconds=settings.strategy_poll_interval_seconds,
        id="strategy_poll",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Strategy scheduler started (interval=%ss)", settings.strategy_poll_interval_seconds)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
