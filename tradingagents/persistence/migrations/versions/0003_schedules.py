"""Add schedules table for recurring unattended analyses.

One row per schedule created on /schedules. Typed columns cover what
the scheduler loop filters on (enabled, next_run_at); tickers, cadence,
time-of-day, selections, and last-run metadata live in the JSONB
payload.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean,
            server_default=sa.text("true"), nullable=False,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "payload", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_schedules_next_run_at", table_name="schedules")
    op.drop_table("schedules")
