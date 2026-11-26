from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("coin", "exchange", name="uq_watch_coin_exchange"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    deposit_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="quiet")
    pushover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timeframes: Mapped[list] = mapped_column(JSONB, nullable=False)
    venues: Mapped[list] = mapped_column(JSONB, nullable=False)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class FlowState(Base):
    __tablename__ = "flow_state"
    __table_args__ = (Index("ix_flow_state_watch", "watch_id"),)

    watch_id: Mapped[int] = mapped_column(
        ForeignKey("watches.id", ondelete="CASCADE"), primary_key=True
    )
    tf: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="NEUTRAL")
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    r: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    z: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confirm_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    marker_trade_ts: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_eval_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prev_dir_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prev_dir_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    delta_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vol_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Bucket(Base):
    __tablename__ = "buckets"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "minute_start", name="uq_bucket"),
        Index("ix_bucket_pair_minute", "exchange", "symbol", "minute_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    minute_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    buy_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sell_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sum_sq: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sell_base: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_event_key"),
        Index("ix_event_watch_type", "watch_id", "type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int | None] = mapped_column(
        ForeignKey("watches.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
