"""Background polling loop: every `STRATEGY_POLL_INTERVAL_SECONDS`, evaluate
every active UserStrategy once. One shared APScheduler instance for the
whole process — started/stopped from app/main.py's lifespan handler.

Each active strategy is evaluated in its own thread (own DB session, own
Dhan HTTP client) via a small pool, not one after another in the main
scheduler thread — `run_user_strategy` makes blocking Dhan API calls, and
evaluating every user's every strategy serially meant one slow/hung call
(e.g. Dhan being sluggish for one user) delayed every other user's
strategy check queued behind it in the same tick, and could make the tick
itself run long enough to eat into the next poll interval.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.engine.daily_summary import send_daily_summaries_for_all_users
from app.engine.runner import run_user_strategy
from app.models import User, UserStrategy

logger = logging.getLogger("app.engine.scheduler")

_scheduler: BackgroundScheduler | None = None

_USER_STRATEGY_LOAD_OPTIONS = (
    selectinload(UserStrategy.user).selectinload(User.dhan_credential),
    selectinload(UserStrategy.strategy),
    selectinload(UserStrategy.runs),
)


def _run_one(user_strategy_id: object) -> None:
    """Re-fetch and evaluate a single UserStrategy on its own DB session —
    each thread gets its own session because SQLAlchemy sessions (and the
    ORM objects loaded through them) aren't safe to share across threads."""
    db = SessionLocal()
    try:
        user_strategy = db.scalars(
            select(UserStrategy)
            .where(UserStrategy.id == user_strategy_id)
            .options(*_USER_STRATEGY_LOAD_OPTIONS)
        ).first()
        if user_strategy is not None:
            run_user_strategy(db, user_strategy)
    except Exception:
        logger.exception("Scheduler tick failed for user_strategy_id=%s", user_strategy_id)
    finally:
        db.close()


def _tick() -> None:
    db = SessionLocal()
    try:
        active_ids = db.scalars(
            select(UserStrategy.id).where(UserStrategy.is_active == True)  # noqa: E712
        ).all()
    except Exception:
        logger.exception("Scheduler tick failed to list active strategies")
        return
    finally:
        db.close()

    if not active_ids:
        return

    max_workers = min(get_settings().strategy_poll_max_workers, len(active_ids))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="strategy-poll") as pool:
        pool.map(_run_one, active_ids)


def _send_daily_summaries() -> None:
    db = SessionLocal()
    try:
        sent = send_daily_summaries_for_all_users(db)
        logger.info("Daily strategy summary: sent %d email(s)", sent)
    except Exception:
        logger.exception("Daily strategy summary job failed")
    finally:
        db.close()


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = (value or "15:35").split(":")
    return int(hour), int(minute)


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
    summary_hour, summary_minute = _parse_hhmm(settings.daily_summary_time)
    _scheduler.add_job(
        _send_daily_summaries,
        "cron",
        hour=summary_hour,
        minute=summary_minute,
        id="daily_summary",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Strategy scheduler started (interval=%ss, daily summary at %02d:%02d IST)",
        settings.strategy_poll_interval_seconds, summary_hour, summary_minute,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
