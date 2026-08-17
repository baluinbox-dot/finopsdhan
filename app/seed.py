"""Idempotent startup seed: makes sure the demo strategy exists so the
paper-trading pipeline is testable immediately after a fresh install."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Strategy
from app.strategies.example_short_strangle import ExampleShortStrangle

logger = logging.getLogger("app.seed")


def seed_demo_strategy() -> None:
    db = SessionLocal()
    try:
        existing = db.scalar(select(Strategy).where(Strategy.code_ref == "example_short_strangle"))
        if existing:
            return
        impl = ExampleShortStrangle()
        db.add(
            Strategy(
                name=impl.name,
                description=impl.description,
                code_ref="example_short_strangle",
                config_schema={},
                default_params=impl.default_params,
                is_published=True,
            )
        )
        db.commit()
        logger.info("Seeded demo strategy: %s", impl.name)
    except Exception:
        db.rollback()
        logger.exception("Could not seed demo strategy (DB may not be migrated yet)")
    finally:
        db.close()
