"""Initial Flow Tracker schema."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("coin", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("deposit_tokens", sa.Float(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("pushover", sa.Boolean(), nullable=False),
        sa.Column("timeframes", postgresql.JSONB(), nullable=False),
        sa.Column("venues", postgresql.JSONB(), nullable=False),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("coin", "exchange", name="uq_watch_coin_exchange"),
    )
    op.create_index("ix_watches_coin", "watches", ["coin"])
    op.create_index("ix_watches_exchange", "watches", ["exchange"])

    op.create_table(
        "flow_state",
        sa.Column("watch_id", sa.Integer(), sa.ForeignKey("watches.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tf", sa.String(8), primary_key=True),
        sa.Column("scope", sa.String(8), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("r", sa.Float(), nullable=False),
        sa.Column("z", sa.Float(), nullable=False),
        sa.Column("confirm_count", sa.Integer(), nullable=False),
        sa.Column("pending_state", sa.String(16), nullable=True),
        sa.Column("marker_trade_ts", sa.Float(), nullable=False),
        sa.Column("last_eval_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prev_dir_state", sa.String(16), nullable=True),
        sa.Column("prev_dir_sec", sa.Float(), nullable=False),
        sa.Column("delta_usd", sa.Float(), nullable=False),
        sa.Column("vol_usd", sa.Float(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_flow_state_watch", "flow_state", ["watch_id"])

    op.create_table(
        "buckets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("minute_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("buy_usd", sa.Float(), nullable=False),
        sa.Column("sell_usd", sa.Float(), nullable=False),
        sa.Column("sum_sq", sa.Float(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("sell_base", sa.Float(), nullable=False),
        sa.UniqueConstraint("exchange", "symbol", "minute_start", name="uq_bucket"),
    )
    op.create_index("ix_bucket_pair_minute", "buckets", ["exchange", "symbol", "minute_start"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("watch_id", sa.Integer(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_event_key"),
    )
    op.create_index("ix_event_watch_type", "events", ["watch_id", "type"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("buckets")
    op.drop_table("flow_state")
    op.drop_table("watches")
