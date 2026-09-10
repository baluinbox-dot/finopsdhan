"""add trade_notes -- a personal daily journal (market view, India VIX,
how today's trades went), one row per user per calendar day
(see app.models.TradeNote / app.routers.notes)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-10

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "trade_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("note_date", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "note_date", name="uq_trade_notes_user_date"),
    )
    # Every list/edit page load looks up "this user's notes, newest
    # first" -- index the lookup path, not just the uniqueness.
    op.create_index("ix_trade_notes_user_id_note_date", "trade_notes", ["user_id", "note_date"])


def downgrade() -> None:
    op.drop_index("ix_trade_notes_user_id_note_date", table_name="trade_notes")
    op.drop_table("trade_notes")
