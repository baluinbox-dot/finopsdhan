"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-17

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "dhan_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("token_saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_profile_snapshot", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("code_ref", sa.String(120), nullable=False),
        sa.Column("config_schema", sa.JSON(), nullable=False),
        sa.Column("default_params", sa.JSON(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "user_strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_strategies_user_id", "user_strategies", ["user_id"])

    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_strategy_id", sa.Uuid(), sa.ForeignKey("user_strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="evaluated"),
        sa.Column("legs_planned", sa.JSON(), nullable=True),
        sa.Column("evaluation_notes", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_strategy_runs_user_strategy_id", "strategy_runs", ["user_strategy_id"])

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_run_id", sa.Uuid(), sa.ForeignKey("strategy_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dhan_order_id", sa.String(64), nullable=True),
        sa.Column("security_id", sa.String(32), nullable=False),
        sa.Column("trading_symbol", sa.String(64), nullable=False, server_default=""),
        sa.Column("transaction_type", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("product_type", sa.String(20), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"])


def downgrade() -> None:
    op.drop_table("orders")
    op.drop_table("strategy_runs")
    op.drop_table("user_strategies")
    op.drop_table("strategies")
    op.drop_table("dhan_credentials")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
