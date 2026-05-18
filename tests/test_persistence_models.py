"""Smoke tests for the ORM models — round-trip every table once.

Gated on TRADINGAGENTS_TEST_DATABASE_URL like test_persistence_engine.
Verifies basic CRUD + the cascade-delete behavior on Run children +
the singleton CHECK on webui_settings.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select, text

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


def _make_run(**kw):
    """Build a Run with sensible defaults; tests override what they need."""
    from tradingagents.persistence import Run

    return Run(
        id=kw.pop("id", uuid.uuid4()),
        ticker=kw.pop("ticker", "NVDA"),
        analysis_date=kw.pop("analysis_date", datetime.date(2026, 5, 17)),
        status=kw.pop("status", "done"),
        selections=kw.pop("selections", {"llm_provider": "openai"}),
        **kw,
    )


def test_run_round_trip(db_engine):
    from tradingagents.persistence import Run, session_scope

    rid = uuid.uuid4()
    with session_scope() as s:
        s.add(_make_run(id=rid, decision="BUY: strong fundamentals", rating="BUY"))

    with session_scope() as s:
        run = s.get(Run, rid)
        assert run is not None
        assert run.ticker == "NVDA"
        assert run.rating == "BUY"
        assert run.selections == {"llm_provider": "openai"}
        # Auto-populated server defaults
        assert run.started_at is not None


def test_run_reports_cascade_on_delete(db_engine):
    """Deleting a Run cascades to its reports / usage / embeddings."""
    from tradingagents.persistence import (
        Run, RunReport, RunTokenUsage, session_scope,
    )

    rid = uuid.uuid4()
    with session_scope() as s:
        s.add(_make_run(id=rid))
        s.add(RunReport(run_id=rid, section="market_report", content="ok"))
        s.add(RunTokenUsage(run_id=rid, usage={"llm_calls": 12}))

    with session_scope() as s:
        run = s.get(Run, rid)
        s.delete(run)

    with session_scope() as s:
        # Children gone via ON DELETE CASCADE.
        assert s.execute(
            text("SELECT count(*) FROM run_reports WHERE run_id = :rid"),
            {"rid": rid},
        ).scalar() == 0
        assert s.execute(
            text("SELECT count(*) FROM run_token_usage WHERE run_id = :rid"),
            {"rid": rid},
        ).scalar() == 0


def test_decision_unique_constraint(db_engine):
    """(ticker, trade_date) is unique — second insert with the same pair errors."""
    from sqlalchemy.exc import IntegrityError

    from tradingagents.persistence import Decision, session_scope

    d = datetime.date(2026, 5, 17)
    with session_scope() as s:
        s.add(Decision(ticker="NVDA", trade_date=d, status="pending", payload={}))

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(Decision(ticker="NVDA", trade_date=d, status="pending", payload={}))


def test_webui_settings_is_singleton(db_engine):
    """The CHECK(id = 1) constraint blocks any second row."""
    from sqlalchemy.exc import IntegrityError

    from tradingagents.persistence import WebUISettings, session_scope

    with session_scope() as s:
        s.add(WebUISettings(id=1, selections={"a": 1}))

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(WebUISettings(id=2, selections={"b": 2}))


def test_run_embedding_round_trip_with_pgvector(db_engine):
    """A vector value round-trips and the HNSW index is queryable."""
    from tradingagents.persistence import Run, RunEmbedding, session_scope
    from tradingagents.persistence.models import EMBEDDING_DIM

    rid = uuid.uuid4()
    # Two deterministic vectors so cosine ordering is predictable.
    v1 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    v2 = [0.9, 0.1] + [0.0] * (EMBEDDING_DIM - 2)
    v3 = [0.0] * (EMBEDDING_DIM - 1) + [1.0]  # orthogonal to v1

    with session_scope() as s:
        s.add(_make_run(id=rid))
        s.add(RunEmbedding(run_id=rid, ticker="NVDA", section="summary",
                           embedding=v1))
        s.add(RunEmbedding(run_id=rid, ticker="NVDA", section="final_trade_decision",
                           embedding=v2))
        s.add(RunEmbedding(run_id=rid, ticker="NVDA", section="investment_plan",
                           embedding=v3))

    # Cosine-similarity ranking: query=v1 → expect v1 closest, then v2, v3 last.
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT section FROM run_embeddings "
            "ORDER BY embedding <=> CAST(:q AS vector) ASC"
        ), {"q": str(v1)}).fetchall()
    sections = [r[0] for r in rows]
    assert sections[0] == "summary"
    assert sections[-1] == "investment_plan"
