"""add data_download_runs -- admin-triggered historical data download runs,
one row per subprocess-executed run of scripts/backtest/
download_historical_data.py (see app.models.DataDownloadRun /
app.routers.admin)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-16

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "data_download_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Looked up on every page load / start-download submit (the
    # one-at-a-time lock scans for status == "running").
    op.create_index("ix_data_download_runs_status", "data_download_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_data_download_runs_status", table_name="data_download_runs")
    op.drop_table("data_download_runs")
