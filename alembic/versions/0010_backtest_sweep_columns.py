"""add sweep_id/sweep_param/sweep_value to backtest_runs -- lets several
rows (one per swept parameter value) share a sweep_id so a parameter
sweep can be run and compared in the app (see app.routers.backtest)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-08

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("sweep_id", sa.Uuid(), nullable=True))
    op.add_column("backtest_runs", sa.Column("sweep_param", sa.String(length=80), nullable=True))
    op.add_column("backtest_runs", sa.Column("sweep_value", sa.Numeric(18, 4), nullable=True))
    # Looked up on every queue-advance tick (app.engine.scheduler) to find
    # a sweep's own undispatched members, and on the sweep comparison page
    # to gather one sweep's rows.
    op.create_index("ix_backtest_runs_sweep_id", "backtest_runs", ["sweep_id"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_sweep_id", table_name="backtest_runs")
    op.drop_column("backtest_runs", "sweep_value")
    op.drop_column("backtest_runs", "sweep_param")
    op.drop_column("backtest_runs", "sweep_id")
