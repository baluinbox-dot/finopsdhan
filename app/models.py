"""ORM models.

Every tenant-scoped table carries a `user_id` (mirrors the `ownerId` pattern
used across Balu's FinOps products). Rows are always filtered by `user_id`
except for the superadmin, who is allowed to see everything — enforced in
`app/deps.py` and the router query helpers, not here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    SUPERADMIN = "superadmin"
    USER = "user"


class StrategyMode(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(str, enum.Enum):
    PLANNED = "planned"
    PLACED = "placed"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    PAPER_FILLED = "paper_filled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), default=UserRole.USER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # New accounts must click the link in a verification email before they
    # can log in. Existing accounts (pre-dating this feature) are backfilled
    # to True by the migration so nobody already registered gets locked out.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verification_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    email_verification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    password_reset_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    dhan_credential: Mapped["DhanCredential | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    user_strategies: Mapped[list["UserStrategy"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def is_superadmin(self) -> bool:
        return self.role == UserRole.SUPERADMIN


class DhanCredential(Base):
    """A user's Dhan API connection. access_token is stored Fernet-encrypted."""

    __tablename__ = "dhan_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    token_saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped["User"] = relationship(back_populates="dhan_credential")


class Strategy(Base):
    """A strategy definition, authored by the superadmin, that users can enable."""

    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # code_ref maps to a registered class in app/strategies/registry.py
    code_ref: Mapped[str] = mapped_column(String(120), nullable=False)

    # JSON schema describing the params a user may configure (lots, offsets, SL%, target%, ...)
    config_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    default_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class UserStrategy(Base):
    """A user's enabled *instance* of a strategy, with its own params/mode.

    A user may have multiple instances of the same Strategy running
    concurrently (e.g. a CE seller and a PE seller both active at once) —
    `label` is how they tell them apart in the UI; there is no uniqueness
    constraint on (user_id, strategy_id)."""

    __tablename__ = "user_strategies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)

    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    mode: Mapped[StrategyMode] = mapped_column(Enum(StrategyMode, native_enum=False), default=StrategyMode.PAPER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    user: Mapped["User"] = relationship(back_populates="user_strategies")
    strategy: Mapped["Strategy"] = relationship()
    runs: Mapped[list["StrategyRun"]] = relationship(back_populates="user_strategy", cascade="all, delete-orphan")


class StrategyRun(Base):
    """One evaluation pass of a user's active strategy by the scheduler."""

    __tablename__ = "strategy_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_strategies.id", ondelete="CASCADE"), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="evaluated", nullable=False)
    legs_planned: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evaluation_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    user_strategy: Mapped["UserStrategy"] = relationship(back_populates="runs")
    orders: Mapped[list["Order"]] = relationship(back_populates="strategy_run", cascade="all, delete-orphan")


class Order(Base):
    """A real or paper order generated by the engine."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategy_runs.id", ondelete="SET NULL"), nullable=True)

    dhan_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY / SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # LIMIT / MARKET / ...
    product_type: Mapped[str] = mapped_column(String(20), nullable=False)  # INTRADAY / MARGIN / ...
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, native_enum=False), default=OrderStatus.PLANNED, nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    strategy_run: Mapped["StrategyRun | None"] = relationship(back_populates="orders")
