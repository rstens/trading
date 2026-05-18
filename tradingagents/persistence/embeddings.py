"""Embedding service + pgvector helpers.

The embedding provider is a **deployment-level** choice (decision #1
conflict in todo.md — pgvector columns have a fixed dim, so different
providers can't share a column). Resolved at first use from config:

  * `embedding_provider` set explicitly → that wins.
  * Otherwise, if `llm_provider` is in {openai, google} → use it.
  * Otherwise → fall back to OpenAI (assumes OPENAI_API_KEY is set;
    logs a warning at construction time).

Switching providers later requires `scripts/reembed.py` (clears + re-embeds
every `done` run with the new provider).

Failure handling: every entry point catches its own exceptions and
returns an empty result / 0-row count rather than failing the caller's
run. A vector-store outage degrades the system to "no RAG context" —
analyses still complete.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import datetime

from sqlalchemy import and_, delete, func, select

from tradingagents.persistence.models import (
    EMBEDDING_DIM,
    Run,
    RunEmbedding,
    RunReport,
)
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.embeddings")


# Sections that get embedded for every successful run. Kept tight so the
# vector store stays focused on actionable text (final reasoning,
# decision, post-pipeline synthesis) rather than raw analyst output.
DEFAULT_EMBED_SECTIONS = ("summary", "final_trade_decision", "investment_plan")


_DEFAULT_MODEL_BY_PROVIDER = {
    "openai": "text-embedding-3-small",   # 1536 dim
    "google": "text-embedding-004",       # 768 dim
}


@dataclass
class SimilarHit:
    """One result row from `find_similar`."""
    run_id: uuid.UUID
    ticker: str
    section: str
    content: str
    distance: float


# ---------- provider resolution ---------------------------------------


def _resolve_provider(cfg: Dict[str, Any]) -> str:
    """Pick the embedding provider per decision #1 + reality of fixed-dim columns."""
    explicit = (cfg.get("embedding_provider") or "").strip().lower()
    if explicit in _DEFAULT_MODEL_BY_PROVIDER:
        return explicit
    if explicit:
        log.warning(
            "Unknown embedding_provider %r — falling back to openai. "
            "Supported in v1: openai, google.", explicit,
        )
        return "openai"

    main = (cfg.get("llm_provider") or "").strip().lower()
    if main in _DEFAULT_MODEL_BY_PROVIDER:
        return main
    if main and main != "openai":
        log.info(
            "Embedding provider auto-resolved to 'openai' (main llm_provider=%s "
            "has no embeddings API). Ensure OPENAI_API_KEY is set or "
            "set TRADINGAGENTS_EMBEDDING_PROVIDER explicitly.", main,
        )
    return "openai"


def _resolve_model(cfg: Dict[str, Any], provider: str) -> str:
    return (
        (cfg.get("embedding_model") or "").strip()
        or _DEFAULT_MODEL_BY_PROVIDER.get(provider, "text-embedding-3-small")
    )


def _build_client(provider: str, model: str):
    """Construct the underlying langchain embeddings client.

    Imported lazily so importing this module doesn't pull in
    langchain_openai / langchain_google_genai unnecessarily.
    """
    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model)
    if provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(model=model)
    # _resolve_provider already guarantees we're in {openai, google}.
    # Belt-and-braces in case a caller bypasses it.
    raise ValueError(f"Unsupported embedding provider: {provider}")


# ---------- the service -----------------------------------------------


class EmbeddingService:
    """Thin wrapper around the configured embedding provider.

    Reads the deployment-level config at construction; not per-call.
    Build one per process and reuse — the underlying langchain client
    keeps its HTTP connection pool open.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        import tradingagents.default_config as dc  # avoid stale capture
        cfg = config if config is not None else dc.DEFAULT_CONFIG
        self.provider: str = _resolve_provider(cfg)
        self.model: str = _resolve_model(cfg, self.provider)
        self.expected_dim: int = int(cfg.get("embedding_dim") or EMBEDDING_DIM)
        self._client = None
        self._client_lock = threading.Lock()
        self._dim_verified = False

    # ----- lazy client init (avoids API-key check at construction) -----

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    log.info(
                        "Embedding client: provider=%s model=%s dim=%d",
                        self.provider, self.model, self.expected_dim,
                    )
                    self._client = _build_client(self.provider, self.model)
        return self._client

    # ----- embedding -----

    def embed_one(self, text: str) -> List[float]:
        """Embed a single string. Raises on API error or dim mismatch."""
        vec = self._get_client().embed_query(text)
        self._assert_dim(vec)
        return vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed many strings. langchain's embed_documents is batched."""
        if not texts:
            return []
        vecs = self._get_client().embed_documents(texts)
        if vecs:
            self._assert_dim(vecs[0])
        return vecs

    def _assert_dim(self, vec: List[float]) -> None:
        """Loud failure when the model returns a vector that doesn't fit
        the pgvector column width. Avoids silent corruption."""
        if self._dim_verified:
            return
        got = len(vec)
        if got != self.expected_dim:
            raise ValueError(
                f"Embedding model {self.model} returned {got}-dim vector "
                f"but the pgvector column is {self.expected_dim}-dim. "
                f"Reconcile: either set TRADINGAGENTS_EMBEDDING_DIM={got} "
                f"and run `scripts/reembed.py` after migrating the column, "
                f"or switch back to the matching model."
            )
        self._dim_verified = True


# Process-wide singleton — embedding clients are stateless across calls
# and benefit from connection reuse.
_service_lock = threading.Lock()
_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Process-singleton EmbeddingService."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = EmbeddingService()
    return _service


def _reset_for_tests() -> None:
    """Drop the cached singleton — test-only utility."""
    global _service
    _service = None


# ---------- persistence -----------------------------------------------


def persist_embeddings_for_run(
    run_id: uuid.UUID,
    ticker: str,
    sections: Dict[str, str],
    service: Optional[EmbeddingService] = None,
) -> int:
    """Embed each non-empty section and insert one `run_embeddings` row each.

    Returns the count of rows actually inserted. Skips empty values
    silently. Per-section embedding failures are caught and logged
    (the call moves on to the next section).

    No-op when DB is unconfigured (session_scope yields None).
    """
    if not sections:
        return 0
    svc = service or get_embedding_service()
    inserted = 0
    try:
        with session_scope() as s:
            if s is None:
                return 0
            for section, content in sections.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                try:
                    vec = svc.embed_one(content)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "embed_one failed for run=%s section=%s: %s",
                        run_id, section, e,
                    )
                    continue
                s.add(RunEmbedding(
                    run_id=run_id,
                    ticker=ticker,
                    section=section,
                    embedding=vec,
                ))
                inserted += 1
    except Exception as e:  # noqa: BLE001
        log.warning("persist_embeddings_for_run failed for %s: %s", run_id, e)
        return 0
    return inserted


def clear_embeddings_for_run(run_id: uuid.UUID) -> int:
    """Drop all embeddings for a single run. Used by reembed for one-off retries."""
    try:
        with session_scope() as s:
            if s is None:
                return 0
            result = s.execute(
                delete(RunEmbedding).where(RunEmbedding.run_id == run_id)
            )
            return result.rowcount or 0
    except Exception as e:  # noqa: BLE001
        log.warning("clear_embeddings_for_run failed for %s: %s", run_id, e)
        return 0


# ---------- similarity search -----------------------------------------


def count_embeddings() -> int:
    """Total rows in `run_embeddings`. Returns 0 when DB is unconfigured.

    Used by the RAG-injection threshold (`get_past_context_db` skips the
    vector lookup entirely until the store has at least a handful of
    embedded runs — keeps early-operation prompts uncluttered).
    """
    try:
        with session_scope() as s:
            if s is None:
                return 0
            return int(s.execute(select(func.count(RunEmbedding.id))).scalar_one())
    except Exception as e:  # noqa: BLE001
        log.warning("count_embeddings failed: %s", e)
        return 0


def find_similar(
    query_text: str,
    ticker: Optional[str] = None,
    sections: Optional[List[str]] = None,
    limit: int = 5,
    since: Optional[datetime.date] = None,
    exclude_run_ids: Optional[List[uuid.UUID]] = None,
    service: Optional[EmbeddingService] = None,
) -> List[SimilarHit]:
    """Cosine-similarity search through `run_embeddings`.

    Joins `run_reports` so each hit comes with its source text. The HNSW
    index on `embedding` accelerates the ORDER BY distance clause. Filters:
      * `ticker` — restrict to a single instrument (None = any).
      * `sections` — restrict to a subset of section keys (None = any).
      * `since` — only consider runs whose `analysis_date >= since`.
      * `exclude_run_ids` — drop matches from these runs (used by
        `get_past_context_db` to avoid duplicating the same-ticker block).
      * `limit` — top-k.
    """
    if not query_text or not query_text.strip():
        return []
    svc = service or get_embedding_service()
    try:
        vec = svc.embed_one(query_text)
    except Exception as e:  # noqa: BLE001
        log.warning("find_similar: query embed failed: %s", e)
        return []

    try:
        with session_scope() as s:
            if s is None:
                return []
            distance_col = RunEmbedding.embedding.cosine_distance(vec).label("distance")
            stmt = (
                select(
                    RunEmbedding.run_id,
                    RunEmbedding.ticker,
                    RunEmbedding.section,
                    RunReport.content,
                    distance_col,
                )
                .outerjoin(
                    RunReport,
                    and_(
                        RunReport.run_id == RunEmbedding.run_id,
                        RunReport.section == RunEmbedding.section,
                    ),
                )
                .order_by(distance_col)
                .limit(limit)
            )
            if ticker:
                stmt = stmt.where(RunEmbedding.ticker == ticker)
            if sections:
                stmt = stmt.where(RunEmbedding.section.in_(list(sections)))
            if since is not None:
                # Date filter requires a Run join. We use INNER join so any
                # orphan embedding rows (shouldn't exist — ON DELETE CASCADE)
                # are excluded anyway.
                stmt = stmt.join(Run, Run.id == RunEmbedding.run_id).where(
                    Run.analysis_date >= since,
                )
            if exclude_run_ids:
                stmt = stmt.where(~RunEmbedding.run_id.in_(list(exclude_run_ids)))

            rows = s.execute(stmt).all()
            return [
                SimilarHit(
                    run_id=run_id,
                    ticker=tkr,
                    section=sec,
                    content=content or "",
                    distance=float(dist),
                )
                for (run_id, tkr, sec, content, dist) in rows
            ]
    except Exception as e:  # noqa: BLE001
        log.warning("find_similar query failed: %s", e)
        return []
