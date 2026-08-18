"""add realized_pnl and closed_at to strategy_runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.add_column("strategy_runs", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))

    # Backfill closed_at for existing closed runs, so pre-existing history
    # shows up in date-wise reports too, not just runs closed from now on.
    # No way to backfill realized_pnl accurately after the fact (would need
    # to re-derive it from each run's orders) — those rows stay at 0;
    # that's an acceptable gap for runs that predate this feature.
    strategy_runs = sa.table(
        "strategy_runs",
        sa.column("status", sa.String()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("closed_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        strategy_runs.update()
        .where(strategy_runs.c.status == "closed")
        .values(closed_at=strategy_runs.c.started_at)
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "closed_at")
    op.drop_column("strategy_runs", "realized_pnl")
