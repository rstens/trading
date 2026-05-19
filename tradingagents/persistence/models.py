"""SQLAlchemy 2.0 ORM models for TradingAgents persistence.

JSONB-heavy per the schema decision: typed columns are reserved for
fields we actually filter / index / join on (`ticker`, `analysis_date`,
`status`, timestamps, and the pgvector embedding). Everything else
— selection blobs, per-section reports, usage stats, debate states —
lives in JSONB, so schema evolution is cheap.

Important: the embedding column's dimension is read from
``DEFAULT_CONFIG["embedding_dim"]`` at module load. Changing the dim
after the table exists requires altering the column (or dropping +
re-creating + re-embedding via the Phase 4 helper script).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.uuid7 import uuid7


# Deployment-level pgvector dim. Read once at import time; changing the
# config later requires a schema migration (or a re-embed pass via
# scripts/reembed.py once that ships in Phase 4).
EMBEDDING_DIM = int(DEFAULT_CONFIG.get("embedding_dim") or 1536)


class Base(DeclarativeBase):
    """Declarative base. Alembic's env.py reads `Base.metadata`."""


class Batch(Base):
    """A queued batch of tickers — one row per submission to /batch."""

    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7,
    )
    # queued | running | paused | done | cancelled
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    submitted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    paused_until: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    # payload = {"base_selections": {...}, "tickers": [
    #     {"ticker": ..., "position": ..., "status": ..., "run_id": ...,
    #      "attempts": ..., "error": ..., "company_name": ...}
    # ]}
    # Per-ticker state changes rewrite this JSONB. Phase 6 mitigates the
    # concurrent-update race with SELECT FOR UPDATE on the row.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    runs: Mapped[list["Run"]] = relationship(back_populates="batch")


class Run(Base):
    """One LangGraph analysis run — the canonical record of what got
    analyzed, by which models, with what outcome."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7,
    )
    ticker: Mapped[str] = mapped_column(String, nullable=False, index=True)
    analysis_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    # Resolved at job start via yfinance.Ticker(...).info. Stored as a
    # typed column (despite the JSONB-heavy preference) because it's a
    # fixed-shape display attribute — not a freeform selection field.
    company_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # queued | running | done | error | cancelled
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("batches.id"), nullable=True,
    )
    # Set when this run was served from cache instead of executed — points
    # to the source run. NULL on fresh runs.
    cached_from_run: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    # All form selections (provider, models, analysts, effort knobs, ...).
    selections: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 5-tier rating extracted from `decision` via parse_rating().
    rating: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    traceback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Full final AgentState — same shape as the on-disk
    # full_states_log_<date>.json. Used by /api/jobs/<id>/export.json.
    raw_state: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Placeholder for the future auth layer. Always NULL today.
    created_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    batch: Mapped[Optional["Batch"]] = relationship(back_populates="runs")
    reports: Mapped[list["RunReport"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
    )
    token_usage: Mapped[Optional["RunTokenUsage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False,
    )
    embeddings: Mapped[list["RunEmbedding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
    )


class RunReport(Base):
    """One row per non-empty section of a run's output. The section key
    matches `webui.runner.TAB_SECTIONS[*][2]` (market_report, summary,
    bull_history, ...)."""

    __tablename__ = "run_reports"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped["Run"] = relationship(back_populates="reports")


class RunTokenUsage(Base):
    """One row per run with the aggregated stats (llm_calls, tool_calls,
    tokens_in, tokens_out — same shape as StatsCallbackHandler.get_stats)."""

    __tablename__ = "run_token_usage"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    run: Mapped["Run"] = relationship(back_populates="token_usage")


class Decision(Base):
    """Replaces the markdown decision log. One row per (ticker, trade_date)
    pair; resolved later with realized returns + reflection on a
    subsequent same-ticker run."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("ticker", "trade_date", name="uq_decisions_ticker_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False, index=True)
    trade_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    # pending | resolved
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # payload = {
    #   "rating": "BUY", "decision_text": "...",
    #   "reflection": "...", "benchmark": "SPY",
    #   "raw_return": 0.013, "alpha_return": 0.004, "holding_days": 5,
    # }
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class RunEmbedding(Base):
    """Vector embeddings of run sections — feeds the RAG injection in
    Phase 5 and the `search_past_analyses` tool. One row per
    (run, embedded section). Dimension is fixed at EMBEDDING_DIM
    (deployment-level config)."""

    __tablename__ = "run_embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False,
    )
    # Denormalized — cheaper to filter than joining through `runs`.
    ticker: Mapped[str] = mapped_column(String, nullable=False, index=True)
    section: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embedded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    run: Mapped["Run"] = relationship(back_populates="embeddings")


# Note: the HNSW index on `embedding` is created by the initial Alembic
# migration via raw `op.execute(...)` because `postgresql_using="hnsw"` +
# custom ops isn't trivially expressible in SQLAlchemy DDL across versions.
# The `ix_run_embeddings_ticker` btree index is auto-generated from
# `index=True` on the column above, so we don't declare it again here.


class WebUISettings(Base):
    """Singleton row holding the last-submitted form values. The
    `id = 1` CHECK ensures there's only ever one row — keeps the data
    model honest until per-user settings land (post-auth)."""

    __tablename__ = "webui_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_webui_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    selections: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
