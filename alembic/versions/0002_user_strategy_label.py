"""add label column to user_strategies (multi-instance support)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "user_strategies",
        sa.Column("label", sa.String(120), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("user_strategies", "label")
