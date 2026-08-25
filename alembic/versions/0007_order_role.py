"""add role to orders, so a hedge and a primary leg sharing the same
underlying contract (same security_id) don't get miswired into a fake
entry/exit pair on the Dashboard

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        # server_default backfills every existing row (whose real role was
        # never persisted) as "primary" -- the overwhelming majority of
        # orders genuinely are; a handful of pre-migration hedge orders
        # will just read as "primary" historically, which only matters for
        # the rare case they happened to share a security_id with another
        # leg (the exact bug this migration fixes going forward).
        sa.Column("role", sa.String(length=20), nullable=False, server_default="primary"),
    )


def downgrade() -> None:
    op.drop_column("orders", "role")
