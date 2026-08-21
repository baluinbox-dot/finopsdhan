"""add superadmin approval gate to users

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-21

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        # server_default true backfills every existing row (registered
        # before this feature existed) as already approved, so nobody
        # already using the app gets locked out. New registrations
        # explicitly set this False in application code (True only for the
        # superadmin account), overriding the column default.
        sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "approved_at")
    op.drop_column("users", "is_approved")
