"""Postgres + pgvector persistence layer for TradingAgents.

Phase 1 (this commit) ships engine + session + ORM models + initial
Alembic migration + docker-compose Postgres service. No production write
paths are wired in yet — that lands in Phase 2.

When ``TRADINGAGENTS_DATABASE_URL`` is unset, the app keeps working in
the legacy mode: in-memory `JobRegistry` / `BatchRegistry`, markdown
decision log (`~/.tradingagents/memory/trading_memory.md`), and the JSON
cache files under ``~/.tradingagents/logs/``. Persistence call sites are
expected to handle `engine = None` gracefully by skipping the write.

Public surface:

    from tradingagents.persistence import (
        Base, Run, RunReport, RunTokenUsage, Decision, Batch,
        RunEmbedding, WebUISettings,
        get_engine, get_session_factory, session_scope,
    )
"""

from tradingagents.persistence.engine import (
    get_engine,
    get_session_factory,
)
from tradingagents.persistence.models import (
    Base,
    Batch,
    Decision,
    Run,
    RunEmbedding,
    RunReport,
    RunTokenUsage,
    Schedule,
    WebUISettings,
)
from tradingagents.persistence.batches import (
    RecentBatchRow,
    get_batch_db,
    insert_batch,
    interrupt_in_flight_batches,
    list_batches_db,
    update_batch_status,
    update_batch_ticker,
)
from tradingagents.persistence.decisions import (
    db_is_configured,
    get_past_context_db,
    get_pending_decisions,
    resolve_decisions,
    store_pending_decision,
)
from tradingagents.persistence.embeddings import (
    DEFAULT_EMBED_SECTIONS,
    EmbeddingService,
    SimilarHit,
    clear_embeddings_for_run,
    count_embeddings,
    find_similar,
    get_embedding_service,
    persist_embeddings_for_run,
)
from tradingagents.persistence.runs import (
    RecentRunRow,
    find_cache_hit_db,
    insert_run,
    interrupt_in_flight_runs,
    list_recent_terminal_runs,
    persist_run_completion,
    update_company_name,
)
from tradingagents.persistence.schedules import (
    delete_schedule_db,
    list_schedules_db,
    upsert_schedule_db,
)
from tradingagents.persistence.settings import (
    load_settings_db,
    save_settings_db,
)
from tradingagents.persistence.session import session_scope

__all__ = [
    # ORM
    "Base",
    "Batch",
    "Decision",
    "Run",
    "RunEmbedding",
    "RunReport",
    "RunTokenUsage",
    "Schedule",
    "WebUISettings",
    # Engine + session
    "get_engine",
    "get_session_factory",
    "session_scope",
    # Runs DAO
    "RecentRunRow",
    "find_cache_hit_db",
    "insert_run",
    "interrupt_in_flight_runs",
    "list_recent_terminal_runs",
    "persist_run_completion",
    "update_company_name",
    # Batches DAO
    "RecentBatchRow",
    "get_batch_db",
    "insert_batch",
    "interrupt_in_flight_batches",
    "list_batches_db",
    "update_batch_status",
    "update_batch_ticker",
    # Decisions DAO
    "db_is_configured",
    "get_past_context_db",
    "get_pending_decisions",
    "resolve_decisions",
    "store_pending_decision",
    # Embeddings + vector store
    "DEFAULT_EMBED_SECTIONS",
    "EmbeddingService",
    "SimilarHit",
    "clear_embeddings_for_run",
    "count_embeddings",
    "find_similar",
    "get_embedding_service",
    "persist_embeddings_for_run",
    # Schedules DAO
    "delete_schedule_db",
    "list_schedules_db",
    "upsert_schedule_db",
    # Settings
    "load_settings_db",
    "save_settings_db",
]
