"""Initial schema: runs, run_reports, run_token_usage, decisions,
batches, run_embeddings, webui_settings + pgvector extension + HNSW
index.

Revision ID: 0001
Revises:
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from tradingagents.default_config import DEFAULT_CONFIG


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Pgvector column width — keep in sync with
# tradingagents.persistence.models.EMBEDDING_DIM (both read the same
# DEFAULT_CONFIG key). Changing this requires altering the column +
# re-embedding existing rows (see scripts/reembed.py, Phase 4).
EMBEDDING_DIM = int(DEFAULT_CONFIG.get("embedding_dim") or 1536)


def upgrade() -> None:
    # The vector type + HNSW index both require the pgvector extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean,
            server_default=sa.text("false"), nullable=False,
        ),
        sa.Column(
            "payload", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.create_index("ix_batches_status", "batches", ["status"])
    op.create_index("ix_batches_submitted_at", "batches", ["submitted_at"])

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String, nullable=False),
        sa.Column("analysis_date", sa.Date, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("batches.id"), nullable=True,
        ),
        sa.Column("cached_from_run", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "selections", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("decision", sa.Text, nullable=True),
        sa.Column("rating", sa.String, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("traceback", sa.Text, nullable=True),
        sa.Column("raw_state", postgresql.JSONB, nullable=True),
        sa.Column("created_by", sa.String, nullable=True),
    )
    op.create_index("ix_runs_ticker", "runs", ["ticker"])
    op.create_index("ix_runs_analysis_date", "runs", ["analysis_date"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_started_at", "runs", ["started_at"])

    op.create_table(
        "run_reports",
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("section", sa.String, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
    )

    op.create_table(
        "run_token_usage",
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "usage", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String, nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.UniqueConstraint("ticker", "trade_date", name="uq_decisions_ticker_date"),
    )
    op.create_index("ix_decisions_ticker", "decisions", ["ticker"])
    op.create_index("ix_decisions_trade_date", "decisions", ["trade_date"])
    op.create_index("ix_decisions_status", "decisions", ["status"])

    op.create_table(
        "run_embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("ticker", sa.String, nullable=False),
        sa.Column("section", sa.String, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "embedded_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_run_embeddings_ticker", "run_embeddings", ["ticker"])
    # HNSW index for cosine similarity. Issued as raw SQL because the
    # combination of `USING hnsw` + opclass isn't expressible across
    # SQLAlchemy versions without dialect-specific quirks.
    op.execute(
        "CREATE INDEX ix_run_embeddings_hnsw "
        "ON run_embeddings USING hnsw (embedding vector_cosine_ops)",
    )

    op.create_table(
        "webui_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "selections", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_webui_settings_singleton"),
    )


def downgrade() -> None:
    op.drop_table("webui_settings")
    op.execute("DROP INDEX IF EXISTS ix_run_embeddings_hnsw")
    op.drop_table("run_embeddings")
    op.drop_table("decisions")
    op.drop_table("run_token_usage")
    op.drop_table("run_reports")
    op.drop_table("runs")
    op.drop_table("batches")
    # Intentionally NOT dropping the vector extension — too destructive
    # for shared databases. Operators can `DROP EXTENSION vector;`
    # manually if needed.
