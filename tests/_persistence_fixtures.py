"""Shared persistence fixtures.

The DB-backed tests are *opt-in*: they only run when
``TRADINGAGENTS_TEST_DATABASE_URL`` is exported. Locally this means
`pytest` works without a Postgres install (these tests get skipped).
CI sets the env var against a `pgvector/pgvector:pg18` service.

Each test gets a freshly-created set of tables via
`Base.metadata.create_all(...)`; the fixture drops them in teardown.
The pgvector extension itself is left in place (idempotent CREATE).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text


def _test_db_url() -> str | None:
    return os.environ.get("TRADINGAGENTS_TEST_DATABASE_URL")


@pytest.fixture
def db_url(monkeypatch) -> str:
    """Override DEFAULT_CONFIG['database_url'] with the test URL.

    Skips when the test URL isn't configured so `pytest` without a
    Postgres container is still green.
    """
    url = _test_db_url()
    if not url:
        pytest.skip("TRADINGAGENTS_TEST_DATABASE_URL not set; skipping DB test")

    # Make the engine factory see the test URL.
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", url)

    # Reset the engine-cache module-globals so the next `get_engine()`
    # call rebuilds against this URL.
    import tradingagents.persistence.engine as eng

    monkeypatch.setattr(eng, "_engine", None)
    monkeypatch.setattr(eng, "_session_factory", None)
    monkeypatch.setattr(eng, "_warned_about_missing_db", False)

    return url


@pytest.fixture
def db_engine(db_url):
    """Build the engine, ensure pgvector + tables exist, drop on teardown.

    Tables are recreated fresh per test for isolation. The pgvector
    extension is left alone — too much state churn to drop+recreate
    every test, and it's idempotent.
    """
    from tradingagents.persistence import Base
    from tradingagents.persistence.engine import get_engine, _reset_for_tests

    # Make sure the extension is installed once before model creation.
    bootstrap = create_engine(db_url)
    with bootstrap.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    bootstrap.dispose()

    engine = get_engine()
    assert engine is not None
    # Drop first so a pre-existing schema (e.g., from a prior alembic
    # upgrade or a previous test run that crashed mid-teardown) doesn't
    # collide with create_all. The HNSW index is dropped with its parent
    # table; we drop it explicitly too in case it survives via a stray
    # statement.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_run_embeddings_hnsw"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # Re-create the HNSW index that lives outside the ORM metadata, so
    # test ordering queries against the vector column work.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_run_embeddings_hnsw "
            "ON run_embeddings USING hnsw (embedding vector_cosine_ops)"
        ))
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_run_embeddings_hnsw"))
            # alembic_version is tracked separately from Base.metadata,
            # so drop_all() leaves it behind — and a later `alembic
            # upgrade` against the same DB would no-op despite tables
            # being gone. Drop it explicitly so the DB is truly clean.
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.drop_all(engine)
        _reset_for_tests()
