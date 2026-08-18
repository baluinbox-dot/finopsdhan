"""add manually_closed flag to strategy_runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-18

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column("manually_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Backfill: runs already closed via "Close Now" (identified the only
    # way available pre-migration — the fixed evaluation_notes string
    # app.engine.runner.close_user_strategy_now has always used) shouldn't
    # retroactively count against today's one-entry-per-day cap either,
    # otherwise this fix wouldn't actually unblock anyone already stuck.
    strategy_runs = sa.table(
        "strategy_runs",
        sa.column("evaluation_notes", sa.Text()),
        sa.column("manually_closed", sa.Boolean()),
    )
    op.execute(
        strategy_runs.update()
        .where(strategy_runs.c.evaluation_notes == "Manually closed by user.")
        .values(manually_closed=True)
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "manually_closed")
