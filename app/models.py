"""ORM models.

Every tenant-scoped table carries a `user_id` (mirrors the `ownerId` pattern
used across Balu's FinOps products). Rows are always filtered by `user_id`
except for the superadmin, who is allowed to see everything — enforced in
`app/deps.py` and the router query helpers, not here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
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

    # A verified account still can't log in until the superadmin approves it
    # (see app.routers.auth.login_submit and app.routers.admin). Superadmin
    # accounts are auto-approved at registration (app.routers.auth). Existing
    # accounts (pre-dating this feature) are backfilled to True by the
    # migration so nobody already registered gets locked out.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    # True only when this run was ended via the "Close Now" button, not an
    # automatic exit (stop-loss/target/window-end/per-leg rule). Purely
    # informational (e.g. for a trade-history view) — it does NOT affect
    # the one-entry-per-day cap; see app.engine.runner._today_run_count.
    manually_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Realized P&L in rupees, accumulated incrementally as legs close (a
    # strategy with independent per-leg exits may close this run across
    # more than one pass) — see app.engine.runner._leg_realized_pnl. Final
    # and correct once status == "closed". closed_at is set the moment
    # that happens, for date-wise reporting.
    realized_pnl: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Combined margin blocked (with hedge benefit), captured once right
    # after entry via app.dhan.helpers.fetch_combined_margin — a snapshot,
    # not continuously updated across later rolls/scale-ins. Margin isn't
    # otherwise available after a run closes (app.engine.pnl.
    # compute_combined_margin only ever looks at *currently open* runs), so
    # without this a post-close report (e.g. the daily summary email) would
    # have no margin figure at all for a run that already squared off.
    # Nullable: None means "couldn't be fetched that pass" (e.g. Dhan call
    # failed), not "zero margin" -- never treat it as 0 in a report.
    entry_margin: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    user_strategy: Mapped["UserStrategy"] = relationship(back_populates="runs")
    orders: Mapped[list["Order"]] = relationship(back_populates="strategy_run", cascade="all, delete-orphan")


class BacktestRun(Base):
    """One in-app backtest request (Phase C). The actual run happens in a
    detached OS subprocess (scripts/backtest/run_single_backtest.py), never
    inside the live-trading uvicorn process -- loading a whole underlying's
    historical CSV cache into memory is a few hundred MB, and this app's
    deploy VM has a documented history of near-OOM incidents. This row is
    the subprocess's only channel back: it writes status/result/
    error_message here as it goes, since there's no request to respond to
    once it's running in the background.

    Deliberately app.routers.backtest enforces "one running/queued row at a
    time, globally" against this table -- not per-user -- since the memory
    risk is shared VM-wide regardless of who asked. See that router for the
    staleness/cooldown safeguards built on top of status/created_at."""

    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)

    underlying: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # The exact params this run used (a copy, not a live reference to the
    # strategy/instance config -- those can change after this row is
    # created, and a past run's result must stay attributable to what it
    # actually ran against).
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # queued -> running -> completed | failed. "queued" has two distinct
    # sub-states distinguished by pid (see below): a lone run or a sweep's
    # first member is dispatched (pid set) the instant it's created and
    # sits "queued" only for the brief window before its own subprocess
    # flips it to "running" -- a row stuck there past the stale threshold
    # is genuinely abandoned. A later sweep member is deliberately created
    # with no pid at all, "queued" for as long as it takes earlier members
    # to finish -- see app.engine.scheduler's queue-advance job, which
    # dispatches it (setting pid) once the lock frees up. Only a
    # *dispatched* (pid IS NOT NULL) queued/running row is ever reaped as
    # stale -- an undispatched sweep member waiting its turn is expected
    # to sit there, sometimes for a while, and must never be reaped.
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Sweep support: several BacktestRun rows sharing one sweep_id are one
    # parameter sweep (see app.routers.backtest's sweep submit/comparison
    # routes) -- sweep_param names which top-level params key varies
    # across them, sweep_value is this row's own value of it. All three
    # None for an ordinary single-value run (not part of any sweep).
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    sweep_param: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sweep_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Summary + trade list + a downsampled (daily) equity curve -- see
    # scripts/backtest/run_single_backtest.py for the exact shape. Never the
    # full tick-by-tick equity curve (thousands of points over a multi-year
    # range) -- that would bloat this row for no real benefit to a summary
    # results page.
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    user: Mapped["User"] = relationship()
    strategy: Mapped["Strategy"] = relationship()


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
    # "primary" | "hedge" (mirrors OrderLeg.role) — bookkeeping only, but
    # load-bearing for app.routers.dashboard._pair_orders: two *different*
    # logical legs (e.g. a hedge and a T/M/B window leg) can legitimately
    # land on the same underlying option contract (same security_id), and
    # grouping by security_id alone would then wire their orders into a
    # fabricated entry/exit pair. Grouping by (security_id, role) too keeps
    # them apart. Existing pre-migration rows default to "primary" (the
    # overwhelming majority) since their real role was never persisted.
    role: Mapped[str] = mapped_column(String(20), default="primary", server_default="primary", nullable=False)

    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, native_enum=False), default=OrderStatus.PLANNED, nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    strategy_run: Mapped["StrategyRun | None"] = relationship(back_populates="orders")
