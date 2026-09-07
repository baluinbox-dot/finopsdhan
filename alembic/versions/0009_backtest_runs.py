"""add backtest_runs -- Phase C in-app backtest requests, one row per
subprocess-executed run (see app.models.BacktestRun / app.routers.backtest)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-07

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("underlying", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Looked up on every backtest submission (the global one-at-a-time
    # lock scans for status in queued/running) -- small table, but this is
    # a hot-path check on a user-facing form submit.
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.create_index("ix_backtest_runs_user_id", "backtest_runs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_user_id", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status", table_name="backtest_runs")
    op.drop_table("backtest_runs")
