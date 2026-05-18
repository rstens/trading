"""Tests for Phase 5 — RAG injection into get_past_context and the
search_past_analyses LangChain tool.

All DB-gated. Uses an in-test embedding-client stub for determinism so
ordering and content excerpts are predictable.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


# ---------- helpers --------------------------------------------------


class _FakeEmbeddings:
    """Deterministic langchain-style embeddings client used in tests."""

    def __init__(self, mapping: dict[str, list[float]], dim: int = 1536):
        # Pad / clip vectors to `dim` so they fit the pgvector column.
        self.mapping = {k: list(v) + [0.0] * (dim - len(v)) for k, v in mapping.items()}
        self.dim = dim

    def embed_query(self, text: str) -> list[float]:
        return list(self.mapping.get(text, [0.0] * self.dim))

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


def _make_service(mapping):
    from tradingagents.persistence.embeddings import EmbeddingService
    from tradingagents.persistence.models import EMBEDDING_DIM

    svc = EmbeddingService({"embedding_provider": "openai",
                            "embedding_dim": EMBEDDING_DIM})
    svc._client = _FakeEmbeddings(mapping, dim=EMBEDDING_DIM)
    return svc


def _seed_run_with_summary(s, ticker: str, summary_text: str,
                          analysis_date=None) -> uuid.UUID:
    """Insert a done Run + a `summary` RunReport. Returns the run id."""
    from tradingagents.persistence import Run, RunReport

    rid = uuid.uuid4()
    s.add(Run(
        id=rid, ticker=ticker,
        analysis_date=analysis_date or datetime.date.today(),
        status="done",
        selections={"ticker": ticker},
    ))
    s.add(RunReport(run_id=rid, section="summary", content=summary_text))
    return rid


def _seed_resolved_decision(s, ticker, days_ago, decision_text, reflection_text):
    """Add a resolved Decision row (drives the same/cross sections)."""
    import datetime as _dt
    from tradingagents.persistence import Decision

    today = _dt.date.today()
    s.add(Decision(
        ticker=ticker,
        trade_date=today - _dt.timedelta(days=days_ago),
        status="resolved",
        payload={
            "rating": "Buy",
            "decision_text": decision_text,
            "reflection": reflection_text,
            "raw_return": 0.012,
            "alpha_return": 0.003,
            "holding_days": 5,
        },
        resolved_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago),
    ))


# ---------- threshold gating ----------------------------------------


def test_rag_block_omitted_when_under_threshold(db_engine, monkeypatch):
    """With < 5 embeddings, the RAG section is suppressed entirely."""
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.decisions import get_past_context_db
    from tradingagents.persistence.embeddings import persist_embeddings_for_run

    # Add 3 same-ticker resolved decisions so the same-ticker section
    # renders; we want to confirm RAG specifically is omitted.
    with session_scope() as s:
        for i in range(3):
            _seed_resolved_decision(s, "NVDA", i, "BUY NVDA", f"good move #{i}")

    # Seed only 2 embeddings — under the default threshold of 5.
    svc = _make_service({"X1 summary": [1, 0, 0], "X2 summary": [0, 1, 0]})
    with session_scope() as s:
        r1 = _seed_run_with_summary(s, "X1", "X1 summary")
        r2 = _seed_run_with_summary(s, "X2", "X2 summary")
    persist_embeddings_for_run(r1, "X1", {"summary": "X1 summary"}, service=svc)
    persist_embeddings_for_run(r2, "X2", {"summary": "X2 summary"}, service=svc)

    # The DB-side get_past_context_db will build its own service — patch
    # the global so the RAG path's count is reachable but the find_similar
    # call would never run because count_embeddings < threshold.
    import tradingagents.persistence.embeddings as emb
    monkeypatch.setattr(emb, "_service", svc)

    ctx = get_past_context_db("NVDA")
    assert "Past analyses of NVDA" in ctx
    assert "Semantically similar past analyses" not in ctx


def test_rag_block_appears_when_enough_embeddings(db_engine, monkeypatch):
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.decisions import get_past_context_db
    from tradingagents.persistence.embeddings import persist_embeddings_for_run

    # Seed 5+ embeddings so the threshold is satisfied.
    summaries = {
        "Bullish AI chips outlook":      [1.0, 0.0, 0.0],
        "Cloud growth strong":           [0.0, 1.0, 0.0],
        "Consumer electronics mixed":    [0.0, 0.0, 1.0],
        "Energy sector volatility":      [0.5, 0.5, 0.0],
        "Healthcare regulatory shifts":  [0.0, 0.5, 0.5],
        # Vector close to the seed text for NVDA:
        "Trading analysis for NVDA":     [0.99, 0.01, 0.0],
    }
    svc = _make_service(summaries)
    monkeypatch.setattr(
        "tradingagents.persistence.embeddings._service", svc,
    )

    with session_scope() as s:
        for ticker, summary in [
            ("OLD1", "Bullish AI chips outlook"),
            ("OLD2", "Cloud growth strong"),
            ("OLD3", "Consumer electronics mixed"),
            ("OLD4", "Energy sector volatility"),
            ("OLD5", "Healthcare regulatory shifts"),
        ]:
            _seed_run_with_summary(s, ticker, summary)

    # Persist embeddings for each summary so they're in the vector store.
    from tradingagents.persistence import Run, session_scope as _scope
    with _scope() as s:
        for run in s.query(Run).order_by(Run.started_at).all():
            persist_embeddings_for_run(
                run.id, run.ticker,
                {"summary": s.query(__import__("tradingagents.persistence",
                                              fromlist=["RunReport"]).RunReport)
                              .filter_by(run_id=run.id).one().content},
                service=svc,
            )

    ctx = get_past_context_db(
        "NVDA",
        company_name="NVIDIA Corporation",
        analysis_date=datetime.date.today().isoformat(),
    )
    # The new RAG section appears.
    assert "Semantically similar past analyses" in ctx
    # Closest to the seed "Trading analysis for NVDA (NVIDIA Corporation) on ..."
    # is "Bullish AI chips outlook" (vector [1, 0, 0]) — should appear.
    assert "OLD1" in ctx or "AI chips" in ctx


# ---------- seed building --------------------------------------------


def test_build_seed_falls_back_to_ticker_alone():
    from tradingagents.persistence.decisions import _build_seed
    assert _build_seed("NVDA", None, None) == "Trading analysis for NVDA"


def test_build_seed_includes_company_and_date():
    from tradingagents.persistence.decisions import _build_seed
    s = _build_seed("NVDA", "NVIDIA Corporation", "2026-05-17")
    assert "NVDA" in s and "NVIDIA Corporation" in s and "2026-05-17" in s


# ---------- find_similar new filters --------------------------------


def test_find_similar_excludes_run_ids(db_engine, monkeypatch):
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import (
        find_similar, persist_embeddings_for_run,
    )

    svc = _make_service({
        "A summary": [1, 0, 0],
        "B summary": [0.9, 0.1, 0],
        "query":     [1, 0, 0],
    })
    with session_scope() as s:
        rA = _seed_run_with_summary(s, "AAA", "A summary")
        rB = _seed_run_with_summary(s, "BBB", "B summary")
    persist_embeddings_for_run(rA, "AAA", {"summary": "A summary"}, service=svc)
    persist_embeddings_for_run(rB, "BBB", {"summary": "B summary"}, service=svc)

    # Without exclusion, AAA wins.
    hits = find_similar("query", service=svc, limit=5)
    assert hits[0].ticker == "AAA"
    # With AAA excluded, BBB wins.
    hits2 = find_similar("query", service=svc, exclude_run_ids=[rA], limit=5)
    assert hits2[0].ticker == "BBB"


def test_find_similar_since_filter(db_engine):
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import (
        find_similar, persist_embeddings_for_run,
    )

    svc = _make_service({
        "Old summary":   [1, 0, 0],
        "Recent summary": [1, 0, 0],
        "query":         [1, 0, 0],
    })
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    long_ago = datetime.date.today() - datetime.timedelta(days=100)
    with session_scope() as s:
        rO = _seed_run_with_summary(s, "OLD", "Old summary", analysis_date=long_ago)
        rN = _seed_run_with_summary(s, "NEW", "Recent summary", analysis_date=yesterday)
    persist_embeddings_for_run(rO, "OLD", {"summary": "Old summary"}, service=svc)
    persist_embeddings_for_run(rN, "NEW", {"summary": "Recent summary"}, service=svc)

    # Without filter: both come back.
    assert {h.ticker for h in find_similar("query", service=svc, limit=5)} == {"OLD", "NEW"}
    # since=today only NEW (NEW.analysis_date=yesterday is NOT >= today;
    # actually let me use yesterday as the cutoff).
    since = yesterday
    hits = find_similar("query", service=svc, since=since, limit=5)
    tickers = {h.ticker for h in hits}
    assert tickers == {"NEW"}


def test_count_embeddings(db_engine):
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import (
        count_embeddings, persist_embeddings_for_run,
    )

    assert count_embeddings() == 0

    svc = _make_service({"x": [1, 0]})
    with session_scope() as s:
        r = _seed_run_with_summary(s, "X", "x")
    persist_embeddings_for_run(r, "X", {"summary": "x"}, service=svc)
    assert count_embeddings() == 1


# ---------- search_past_analyses tool --------------------------------


def test_search_past_analyses_returns_formatted_markdown(db_engine, monkeypatch):
    from tradingagents.agents.utils.agent_utils import search_past_analyses
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import persist_embeddings_for_run

    svc = _make_service({
        "NVDA chips":  [1, 0, 0],
        "AAPL phones": [0, 1, 0],
        "AI semis":    [0.95, 0.05, 0],
    })
    monkeypatch.setattr(
        "tradingagents.persistence.embeddings._service", svc,
    )

    with session_scope() as s:
        _seed_run_with_summary(s, "NVDA", "NVDA chips")
        _seed_run_with_summary(s, "AAPL", "AAPL phones")
    from tradingagents.persistence import Run
    with session_scope() as s:
        for r in s.query(Run).all():
            persist_embeddings_for_run(
                r.id, r.ticker,
                {"summary": "NVDA chips" if r.ticker == "NVDA" else "AAPL phones"},
                service=svc,
            )

    # The `@tool` decorator wraps as a StructuredTool. Use `.invoke` per
    # langchain's tool API.
    out = search_past_analyses.invoke({"query": "AI semis", "limit": 5})
    assert out, "expected markdown output, got empty"
    # NVDA is closer to AI semis than AAPL.
    assert out.find("NVDA") < out.find("AAPL")
    assert "cos=" in out


def test_search_past_analyses_ticker_filter(db_engine, monkeypatch):
    from tradingagents.agents.utils.agent_utils import search_past_analyses
    from tradingagents.persistence import session_scope, Run
    from tradingagents.persistence.embeddings import persist_embeddings_for_run

    svc = _make_service({"a": [1, 0], "b": [0, 1], "q": [1, 0]})
    monkeypatch.setattr(
        "tradingagents.persistence.embeddings._service", svc,
    )
    with session_scope() as s:
        _seed_run_with_summary(s, "AAA", "a")
        _seed_run_with_summary(s, "BBB", "b")
    with session_scope() as s:
        for r in s.query(Run).all():
            persist_embeddings_for_run(
                r.id, r.ticker,
                {"summary": "a" if r.ticker == "AAA" else "b"},
                service=svc,
            )

    out = search_past_analyses.invoke({"query": "q", "ticker": "BBB"})
    assert "BBB" in out and "AAA" not in out


def test_search_past_analyses_invalid_since_returns_error_string():
    from tradingagents.agents.utils.agent_utils import search_past_analyses
    out = search_past_analyses.invoke({"query": "x", "since": "not a date"})
    assert "invalid" in out.lower()


def test_search_past_analyses_empty_query_returns_empty():
    from tradingagents.agents.utils.agent_utils import search_past_analyses
    assert search_past_analyses.invoke({"query": ""}) == ""
    assert search_past_analyses.invoke({"query": "   "}) == ""
