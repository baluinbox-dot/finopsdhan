"""add entry_margin to strategy_runs -- a snapshot of combined margin
blocked, captured once at entry, so a post-close report (the daily
summary email) still has a margin figure for a run that already squared
off (margin is otherwise only ever computed live for currently-open runs)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-03

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        # Nullable, no server_default -- every pre-migration run simply has
        # no captured margin (None, not 0; a report must treat that as
        # "unknown", never as "zero margin was used").
        sa.Column("entry_margin", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "entry_margin")
