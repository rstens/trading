"""Alembic environment.

Reads the DB URL from the app's `DEFAULT_CONFIG` (i.e. from
``TRADINGAGENTS_DATABASE_URL`` via the env-var overlay), so a single
source of truth governs both the running app and the migration tool.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the project root is on sys.path so `tradingagents.*` is importable
# whether alembic is invoked from the wrapper (__main__.py) or directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent.parent))

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.persistence.models import Base  # noqa: E402


# Alembic Config object — gives access to alembic.ini.
config = context.config

# Set up logging from alembic.ini if it has a config_file_name.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Target metadata for autogenerate. Includes every table declared in
# tradingagents.persistence.models because of the central Base.
target_metadata = Base.metadata


def _resolve_db_url() -> str:
    url = DEFAULT_CONFIG.get("database_url")
    if not url:
        raise RuntimeError(
            "TRADINGAGENTS_DATABASE_URL must be set to run migrations. "
            "Export it (or add it to .env) before running "
            "`python -m tradingagents.persistence upgrade`.",
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations without a live connection — useful for generating SQL."""
    context.configure(
        url=_resolve_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _resolve_db_url()
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare types when autogenerating so JSONB / pgvector
            # additions show up cleanly.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
