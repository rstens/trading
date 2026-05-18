"""Tests for the embedding service + pgvector helpers.

* Pure-Python tests (no DB) for provider resolution, model defaults,
  dimension assertion.
* DB-gated tests (skipped without TRADINGAGENTS_TEST_DATABASE_URL) that
  exercise persist + cosine-similarity ordering via a fake embedding
  client that returns deterministic vectors.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


# ---------- A fake embedding client for tests ------------------------


class _FakeEmbeddings:
    """Deterministic stand-in for langchain's *Embeddings clients.

    Maps each text to a fixed vector via the constructor's `mapping`
    dict; falls back to a zero vector with a 1.0 in the first slot for
    anything unseen (lets us produce orthogonal "different topic" hits).
    """

    def __init__(self, mapping: dict[str, list[float]], dim: int = 1536):
        self.mapping = mapping
        self.dim = dim

    def embed_query(self, text: str) -> list[float]:
        if text in self.mapping:
            return list(self.mapping[text])
        # Default: orthogonal-to-everything vector.
        return [0.0] * self.dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


# ---------- Provider resolution (no DB) ------------------------------


def test_resolve_provider_explicit_openai():
    from tradingagents.persistence.embeddings import _resolve_provider
    assert _resolve_provider({"embedding_provider": "openai"}) == "openai"


def test_resolve_provider_explicit_google():
    from tradingagents.persistence.embeddings import _resolve_provider
    assert _resolve_provider({"embedding_provider": "google"}) == "google"


def test_resolve_provider_unknown_falls_back_to_openai(caplog):
    from tradingagents.persistence.embeddings import _resolve_provider
    with caplog.at_level("WARNING", logger="tradingagents.persistence.embeddings"):
        got = _resolve_provider({"embedding_provider": "voyage"})
    assert got == "openai"
    assert any("voyage" in r.message.lower() for r in caplog.records)


def test_resolve_provider_auto_follows_main_when_openai():
    from tradingagents.persistence.embeddings import _resolve_provider
    assert _resolve_provider({"llm_provider": "openai"}) == "openai"


def test_resolve_provider_auto_follows_main_when_google():
    from tradingagents.persistence.embeddings import _resolve_provider
    assert _resolve_provider({"llm_provider": "google"}) == "google"


def test_resolve_provider_auto_falls_back_when_anthropic():
    """Anthropic has no embeddings API → fall back to openai."""
    from tradingagents.persistence.embeddings import _resolve_provider
    assert _resolve_provider({"llm_provider": "anthropic"}) == "openai"


def test_resolve_model_uses_provider_default():
    from tradingagents.persistence.embeddings import _resolve_model
    assert _resolve_model({}, "openai") == "text-embedding-3-small"
    assert _resolve_model({}, "google") == "text-embedding-004"


def test_resolve_model_honors_explicit_override():
    from tradingagents.persistence.embeddings import _resolve_model
    assert _resolve_model(
        {"embedding_model": "text-embedding-3-large"}, "openai",
    ) == "text-embedding-3-large"


# ---------- EmbeddingService construction + dim assertion -------------


def _patch_service_client(svc, client):
    """Inject a fake client + skip the "log on first build" overhead."""
    svc._client = client


def test_embedding_service_picks_up_config_at_construction(monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.persistence.embeddings import EmbeddingService

    monkeypatch.setitem(DEFAULT_CONFIG, "embedding_provider", "openai")
    monkeypatch.setitem(DEFAULT_CONFIG, "embedding_model", "text-embedding-3-small")
    monkeypatch.setitem(DEFAULT_CONFIG, "embedding_dim", 1536)
    svc = EmbeddingService()
    assert svc.provider == "openai"
    assert svc.model == "text-embedding-3-small"
    assert svc.expected_dim == 1536


def test_embedding_service_assert_dim_raises_on_mismatch():
    from tradingagents.persistence.embeddings import EmbeddingService

    svc = EmbeddingService({"embedding_provider": "openai", "embedding_dim": 1536})
    _patch_service_client(svc, _FakeEmbeddings({"hi": [0.1, 0.2, 0.3]}))
    with pytest.raises(ValueError) as exc:
        svc.embed_one("hi")
    assert "1536-dim" in str(exc.value)
    assert "TRADINGAGENTS_EMBEDDING_DIM" in str(exc.value)


def test_embedding_service_embed_one_returns_vector():
    from tradingagents.persistence.embeddings import EmbeddingService

    svc = EmbeddingService({"embedding_provider": "openai", "embedding_dim": 3})
    _patch_service_client(svc, _FakeEmbeddings({"hi": [0.1, 0.2, 0.3]}, dim=3))
    vec = svc.embed_one("hi")
    assert vec == [0.1, 0.2, 0.3]


def test_embedding_service_embed_batch():
    from tradingagents.persistence.embeddings import EmbeddingService

    svc = EmbeddingService({"embedding_provider": "openai", "embedding_dim": 3})
    _patch_service_client(svc, _FakeEmbeddings(
        {"a": [1, 0, 0], "b": [0, 1, 0]}, dim=3,
    ))
    vecs = svc.embed_batch(["a", "b"])
    assert vecs == [[1, 0, 0], [0, 1, 0]]


# ---------- DB-gated: persist + similarity ---------------------------


def _make_done_run(s, ticker: str, run_uuid: uuid.UUID, sections: dict):
    """Insert a minimal `done` Run + its reports."""
    from tradingagents.persistence import Run, RunReport
    s.add(Run(
        id=run_uuid,
        ticker=ticker,
        analysis_date=datetime.date.today(),
        status="done",
        selections={"ticker": ticker},
    ))
    for section, content in sections.items():
        s.add(RunReport(run_id=run_uuid, section=section, content=content))


def _make_service_with_fake_client(mapping: dict[str, list[float]]):
    """Build an EmbeddingService that returns deterministic vectors.

    Uses dim=`EMBEDDING_DIM` from the model module so the vectors are
    accepted by the pgvector column.
    """
    from tradingagents.persistence.embeddings import EmbeddingService
    from tradingagents.persistence.models import EMBEDDING_DIM

    # Pad shorter vectors with zeros to EMBEDDING_DIM.
    padded = {
        text: list(vec) + [0.0] * (EMBEDDING_DIM - len(vec))
        for text, vec in mapping.items()
    }
    svc = EmbeddingService(
        {"embedding_provider": "openai", "embedding_dim": EMBEDDING_DIM},
    )
    _patch_service_client(svc, _FakeEmbeddings(padded, dim=EMBEDDING_DIM))
    return svc


def test_persist_embeddings_inserts_one_row_per_nonempty_section(db_engine):
    from tradingagents.persistence import (
        RunEmbedding, session_scope,
    )
    from tradingagents.persistence.embeddings import persist_embeddings_for_run

    rid = uuid.uuid4()
    with session_scope() as s:
        _make_done_run(s, "NVDA", rid, {"summary": "Buy NVDA."})

    svc = _make_service_with_fake_client({"Buy NVDA.": [1.0, 0.0, 0.0]})
    n = persist_embeddings_for_run(rid, "NVDA", {
        "summary": "Buy NVDA.",
        "final_trade_decision": "",   # skipped
        "investment_plan": "",        # skipped
    }, service=svc)
    assert n == 1

    with session_scope() as s:
        rows = s.query(RunEmbedding).filter_by(run_id=rid).all()
        assert len(rows) == 1
        assert rows[0].section == "summary"
        assert rows[0].ticker == "NVDA"
        # First three coordinates match what the fake client returned.
        assert list(rows[0].embedding[:3]) == [1.0, 0.0, 0.0]


def test_persist_embeddings_swallows_api_errors_per_section(db_engine):
    """If embedding one section raises, the others still get persisted."""
    from tradingagents.persistence import RunEmbedding, session_scope
    from tradingagents.persistence.embeddings import (
        EmbeddingService, persist_embeddings_for_run,
    )
    from tradingagents.persistence.models import EMBEDDING_DIM

    rid = uuid.uuid4()
    with session_scope() as s:
        _make_done_run(s, "NVDA", rid, {"summary": "Buy", "investment_plan": "Plan"})

    class _FlakyClient:
        def embed_query(self, text):
            if text == "Buy":
                raise RuntimeError("transient API error")
            return [0.0] * (EMBEDDING_DIM - 1) + [1.0]
        def embed_documents(self, texts):
            return [self.embed_query(t) for t in texts]

    svc = EmbeddingService(
        {"embedding_provider": "openai", "embedding_dim": EMBEDDING_DIM},
    )
    _patch_service_client(svc, _FlakyClient())

    n = persist_embeddings_for_run(rid, "NVDA", {
        "summary": "Buy",            # raises → skipped
        "investment_plan": "Plan",   # succeeds
    }, service=svc)
    assert n == 1

    with session_scope() as s:
        rows = s.query(RunEmbedding).filter_by(run_id=rid).all()
        assert len(rows) == 1
        assert rows[0].section == "investment_plan"


def test_find_similar_orders_by_cosine_distance(db_engine):
    """Closest vector first, joins back to RunReport content."""
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import (
        find_similar, persist_embeddings_for_run,
    )

    r1 = uuid.uuid4()
    r2 = uuid.uuid4()
    r3 = uuid.uuid4()

    with session_scope() as s:
        _make_done_run(s, "NVDA", r1, {"summary": "Bullish on AI chips."})
        _make_done_run(s, "AAPL", r2, {"summary": "Mixed signals on services."})
        _make_done_run(s, "MSFT", r3, {"summary": "Cloud growth remains strong."})

    svc = _make_service_with_fake_client({
        "Bullish on AI chips.":         [1.0, 0.0, 0.0],
        "Mixed signals on services.":   [0.0, 1.0, 0.0],
        "Cloud growth remains strong.": [0.0, 0.0, 1.0],
        "AI semiconductors":            [0.95, 0.05, 0.0],   # close to r1
    })
    # Persist all three runs' embeddings.
    persist_embeddings_for_run(r1, "NVDA", {"summary": "Bullish on AI chips."}, service=svc)
    persist_embeddings_for_run(r2, "AAPL", {"summary": "Mixed signals on services."}, service=svc)
    persist_embeddings_for_run(r3, "MSFT", {"summary": "Cloud growth remains strong."}, service=svc)

    # Query with a vector closest to r1's.
    hits = find_similar("AI semiconductors", limit=3, service=svc)
    assert len(hits) == 3
    # NVDA must come first (smallest cosine distance).
    assert hits[0].ticker == "NVDA"
    assert hits[0].content == "Bullish on AI chips."
    # Distances are monotone non-decreasing.
    assert hits[0].distance <= hits[1].distance <= hits[2].distance


def test_find_similar_filters_by_ticker(db_engine):
    from tradingagents.persistence import session_scope
    from tradingagents.persistence.embeddings import (
        find_similar, persist_embeddings_for_run,
    )

    r1 = uuid.uuid4()
    r2 = uuid.uuid4()
    with session_scope() as s:
        _make_done_run(s, "NVDA", r1, {"summary": "NVDA summary"})
        _make_done_run(s, "AAPL", r2, {"summary": "AAPL summary"})

    svc = _make_service_with_fake_client({
        "NVDA summary": [1.0, 0.0],
        "AAPL summary": [0.0, 1.0],
        "query":        [1.0, 0.0],
    })
    persist_embeddings_for_run(r1, "NVDA", {"summary": "NVDA summary"}, service=svc)
    persist_embeddings_for_run(r2, "AAPL", {"summary": "AAPL summary"}, service=svc)

    hits = find_similar("query", ticker="AAPL", service=svc)
    assert {h.ticker for h in hits} == {"AAPL"}


def test_find_similar_returns_empty_on_blank_query(db_engine):
    from tradingagents.persistence.embeddings import find_similar
    assert find_similar("") == []
    assert find_similar("   ") == []


def test_clear_embeddings_for_run(db_engine):
    from tradingagents.persistence import RunEmbedding, session_scope
    from tradingagents.persistence.embeddings import (
        clear_embeddings_for_run, persist_embeddings_for_run,
    )

    rid = uuid.uuid4()
    with session_scope() as s:
        _make_done_run(s, "NVDA", rid, {"summary": "x", "final_trade_decision": "y"})

    svc = _make_service_with_fake_client({"x": [1.0, 0.0], "y": [0.0, 1.0]})
    persist_embeddings_for_run(rid, "NVDA", {
        "summary": "x", "final_trade_decision": "y",
    }, service=svc)

    n = clear_embeddings_for_run(rid)
    assert n == 2

    with session_scope() as s:
        assert s.query(RunEmbedding).filter_by(run_id=rid).count() == 0
