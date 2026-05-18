"""Add company_name column to runs.

The runner resolves it via yfinance.Ticker(...).info at job start; we
store it so cache hits and Recent-jobs entries render the full name
without re-hitting yfinance.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("company_name", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "company_name")
