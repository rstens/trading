# TODO — Postgres + pgvector persistence

Phased work plan. Each `[ ]` is intended to be one focused commit / PR-sized
change. Phases ship independently; the DB-off fallback never goes away.

## Decisions on record

1. **Embedding provider**: deployment-level config. Default = main pipeline
   provider if it has embeddings (OpenAI, Google), else OpenAI. Switching
   later requires a re-embed pass (Phase 4 ships the script).
2. **Markdown decision log**: sunset after the Phase 3 backfill. No
   forever write-through mirror.
3. **Backfill scope**: only `~/.tradingagents/memory/trading_memory.md`.
   `full_states_log_*.json` and `webui_cache_*.json` stay on disk; not imported.
4. **No-DB ergonomics**: `tradingagents serve` starts happily with
   `TRADINGAGENTS_DATABASE_URL` unset. Logs a single warning at startup,
   persists nothing, falls back to in-memory state + markdown log
   (until Phase 3 sunsets it — at that point the no-DB fallback uses the
   markdown log only for read).
5. **Auth**: deferred. No per-user filtering in v1.
6. **Schema style**: JSONB-heavy. Typed columns only for fields we
   filter / join / index on (`ticker`, `analysis_date`, `status`,
   timestamps, the embedding vector). Everything else (selections,
   reports, usage, debate states) lives in JSONB.
7. **RAG defaults**: cross-ticker industry context included by default
   in PM prompt augmentation. The `search_past_analyses` tool defaults
   to "any ticker" but accepts a filter.
8. **Postgres version**: **18+** (newer HNSW/pgvector defaults). Compose
   pins `pgvector/pgvector:pg18`; README states the min server version.
9. **Recent jobs UI**: **hybrid**. In-memory `JobRegistry` is the source
   of truth for live (`running` / `queued`) jobs; DB is canonical for
   terminal rows. Index page merges by `id` (registry wins on
   collision) and sorts by `started_at DESC`.
10. **Full AgentState**: kept in `runs.raw_state jsonb` even at
    50–500 KB per run. Acceptable for Postgres TOAST.

## Cross-cutting conventions

- All DB writes go through a `with session_scope():` context manager.
  Each runner thread / batch worker gets its own SQLAlchemy session.
- All persistence sites are wrapped: if `database_url` is unset OR a write
  raises, log a warning and continue. The run / batch never fails because of DB.
- Schema migrations go through Alembic only. No hand-rolled DDL after Phase 1.
- New env vars are added to `tradingagents/default_config.py::_ENV_OVERRIDES`.
- New deps versioned in `pyproject.toml` with floor pins; lockfile updated.

---

## Phase 1 — Foundation (DB + ORM + scaffolding) ✅

- [x] **deps**: added `psycopg[binary]>=3.2`, `sqlalchemy>=2.0`,
      `alembic>=1.13`, `pgvector>=0.3` to `pyproject.toml`.
- [x] **package**: `tradingagents/persistence/{__init__,engine,session,models}.py`.
      Engine factory returns None when URL unset (warns once);
      `session_scope()` yields None or a session, commits/rollbacks.
- [x] **config keys**: 4 new entries in `_ENV_OVERRIDES` +
      DEFAULT_CONFIG defaults.
- [x] **schema (models.py)**: all 7 tables declared. `created_by` placeholder
      on `runs`. JSONB-heavy per decision #6.
- [x] **Alembic baseline**: `0001_initial.py` migration creates the
      vector extension, all 7 tables, and the HNSW index. Driven by
      `python -m tradingagents.persistence upgrade`.
- [x] **docker-compose**: `postgres` service on `pgvector/pgvector:pg18`
      with healthcheck, scoped under the `persistence` profile so the
      legacy flow isn't disturbed. `.env.example` documents the env var.
- [x] **pytest fixtures**: `tests/_persistence_fixtures.py` —
      `db_url` + `db_engine` fixtures, skip when
      `TRADINGAGENTS_TEST_DATABASE_URL` unset.
- [x] **test**: `tests/test_persistence_engine.py` (6 tests, 3 no-DB +
      3 DB-gated) + `tests/test_persistence_models.py` (5 tests
      covering Run round-trip, cascade delete, decisions unique,
      webui_settings singleton, pgvector cosine ordering).
- [x] **smoke**: verified end-to-end against a live
      `pgvector/pgvector:pg18` container — migration runs, all 11
      tests pass, no regressions in the existing test suite.

## Phase 2 — Runs persistence (write + read) ✅

Implemented as `tradingagents/persistence/runs.py` (DAO module) +
`webui/runner.py` integrations + `webui/routes.py` hybrid view +
migration 0002 (`company_name` column).

- [x] **insert on start**: `webui/runner.py::_run` calls
      `insert_run(...)` before anything else; the returned UUID is
      stored on `JobState.db_run_id`. URL stays at 12-char hex
      (`JobState.id`) — DB UUID is the secondary key.
- [x] **update on terminal**: `persist_run_completion(...)` updates
      the row in one transaction across all three exit paths (done /
      error / cancelled). Wraps decision-text rating extraction via
      `parse_rating`. Cancelled / error runs persist partial reports
      too via `_persist_terminal_no_cache`.
- [x] **per-section reports**: same transaction inserts one
      `run_reports` row per non-empty section in `partial_state`
      (covers `summary`, all analyst sections, and flattened debate
      states).
- [x] **token usage**: same transaction upserts a single
      `run_token_usage` row via `Session.merge(...)`.
- [x] **cache lookup**: `JobRegistry.start()` now tries
      `find_cache_hit_db(...)` first; file cache remains as a
      degraded fallback for both no-DB mode and legacy on-disk
      caches.
- [x] **Recent jobs (hybrid per decision #9)**:
      `routes.py::_merged_recent_jobs` overlays registry rows with DB
      terminal rows, dedupes by `db_run_id` (registry wins), sorts by
      `started_at DESC`, slices to 20. `running_count` still sourced
      from the registry as the live source of truth.
- [x] **job detail when running**: `job_detail` + `htmx_job_status`
      both fall back to `_load_job_from_db(job_id)` when the registry
      doesn't have the row — DB UUID URLs work after restart and for
      any rolled-off run.
- [x] **test**: `tests/test_persistence_runs.py` — 8 tests covering
      insert round-trip, terminal persistence (status/rating/reports/
      usage), no-DB fallback, cache-hit success / TTL miss / non-done
      miss, and `list_recent_terminal_runs` ordering.
- [x] **smoke**: end-to-end against a live `pgvector/pgvector:pg18`
      container — seeded a DB-only run, hit `/`, `/jobs/<uuid>`, and
      `/htmx/jobs/<uuid>/status`, all rendered correctly. 19/19
      persistence tests pass, 206/206 full suite passes (zero
      regressions).

## Phase 3 — Decisions table + markdown sunset ✅

Implemented as `tradingagents/persistence/decisions.py` (DAO) +
`TradingMemoryLog` delegation + `scripts/backfill_decisions.py`. Markdown
writes are skipped entirely when DB is configured (decision #2 sunset
behavior — no double-write transition).

- [x] **schema sanity**: existing `decisions(payload jsonb)` accommodates
      the rating / decision_text / reflection / raw_return / alpha_return /
      holding_days / benchmark fields. UNIQUE(ticker, trade_date)
      provides idempotency for backfill + double-store-decision.
- [x] **DAO**: `store_pending_decision`, `resolve_decisions`,
      `get_pending_decisions`, `get_past_context_db`, `db_is_configured`.
- [x] **TradingMemoryLog delegation**: `store_decision`,
      `get_pending_entries`, `batch_update_with_outcomes`,
      `update_with_outcome`, `get_past_context` all route to DB when
      configured, fall back to markdown when not. Skip markdown write
      entirely when DB is on (sunset).
- [x] **backfill script**: `scripts/backfill_decisions.py` parses the
      markdown log via `TradingMemoryLog.load_entries` (reuses the
      existing tag/body parser), inserts as pending or resolved rows
      with parsed numeric returns / holding days. Idempotent on
      `UNIQUE(ticker, trade_date)`. Smoke-tested twice in a row → 2
      inserted then 0 inserted / 2 skipped.
- [x] **test**: `tests/test_decision_store.py` — 12 tests covering DAO
      operations, delegation contract, no-DB fallback, and backfill
      round-trip + idempotency.
- [x] **bug fix**: `engine.py` was capturing a stale reference to
      `DEFAULT_CONFIG` at module import, breaking when
      `test_env_overrides.py` does `importlib.reload(default_config)`.
      Fixed by reading from the module object on every call
      (`_current_db_url`). Full suite now 233 pass + 1 skip with DB,
      208 pass + 26 skip without.
- [x] **fixture fix**: `test_memory_log.py` autouses a
      `_force_markdown_path` fixture so the legacy markdown tests
      still hit the markdown branch even when DB env is set.

## Phase 4 — Embeddings + vector store ✅

Implemented as `tradingagents/persistence/embeddings.py` + runner
hook + `scripts/reembed.py`. Vector store stays consistent with the
parent `runs` row via the existing `ON DELETE CASCADE`.

- [x] **EmbeddingService**: lazy langchain-client init; dim assertion
      on first call; `embed_one` / `embed_batch`. Process-singleton
      via `get_embedding_service()`.
- [x] **Provider routing**: `openai` (default), `google`. Unknown
      providers + Anthropic / xAI / etc. main pipelines fall back to
      openai with a logged note. Resolved at construction.
- [x] **Auto-default**: when `embedding_provider` is unset,
      `_resolve_provider` follows `llm_provider` if openai/google,
      else openai. Resolved choice logged on first client build.
- [x] **Dimension assertion**: first call asserts vector length
      matches `embedding_dim`; explicit error with a remediation hint
      pointing at `scripts/reembed.py` if there's a mismatch.
- [x] **End-of-pipeline hook**: `webui/runner.py::_run` calls
      `persist_embeddings_for_run` for `summary`,
      `final_trade_decision`, `investment_plan` after the canonical
      DB write succeeds. Non-fatal on embedding API error — per-section
      try/except inside `persist_embeddings_for_run`.
- [x] **Similarity query**: `find_similar(query, ticker, sections,
      limit)` returns `SimilarHit(run_id, ticker, section, content,
      distance)`. Joins `run_reports` for content; uses pgvector's
      `cosine_distance` operator which lights up the HNSW index from
      migration 0001.
- [x] **Re-embed script**: `scripts/reembed.py` snapshots all done
      runs' embed-target sections, optionally wipes existing rows,
      then re-embeds via the currently-configured provider.
      Per-run try/except so a flaky cell doesn't stop the rest.
- [x] **Tests**: `tests/test_embeddings.py` — 18 tests covering
      provider resolution (no DB), dimension assertion, per-section
      API-error tolerance, cosine-distance ordering, ticker filter,
      `clear_embeddings_for_run`. Plus end-to-end smoke against a
      live `pgvector/pgvector:pg18` container with patched embedding
      client.
- [x] **regression**: full suite 251 pass + 1 skip (DB on), 220 pass
      + 32 skip (DB off). No regressions.

## Phase 5 — RAG injection + LLM tool ✅ (partial — manager tool-binding deferred)

Implemented as `decisions.py::get_past_context_db` augmentation,
extended `find_similar` (since + exclude_run_ids), `count_embeddings`,
and the `search_past_analyses` LangChain tool. Manager tool-binding
DEFERRED — see "follow-ups" below.

- [x] **`get_past_context_db` augmentation**: composes 3 blocks (same
      / cross / vector). Vector lookup is gated on a `count_embeddings()
      >= vector_min_threshold` (default 5) so early-operation prompts
      stay uncluttered.
- [x] **Deterministic seed**: `_build_seed(ticker, company_name?,
      analysis_date?)` → `"Trading analysis for TICKER (COMPANY) on
      DATE"`. Falls back to ticker-only when context not provided.
- [x] **Plumbing**: `TradingMemoryLog.get_past_context` accepts
      `company_name` / `analysis_date` keyword args; `TradingAgentsGraph._run_graph`
      pulls `company_name` from a graph attribute set by the runner.
      None values harmless — seed degrades gracefully.
- [x] **`find_similar` enhancements**: new `since` and `exclude_run_ids`
      filters. Joins `Run` for date filtering (the existing HNSW + Run
      FK are intact).
- [x] **`count_embeddings()`**: cheap COUNT used by the threshold gate.
- [x] **`search_past_analyses` `@tool`**: in `agents/utils/agent_utils.py`.
      Signature: `search_past_analyses(query, ticker?, since?, limit=5)`.
      Returns markdown-formatted ranked list. Validates `since`,
      caps `limit` at 25, empty query → empty string. Callable from
      any analyst subgraph that already has a tool loop (Phase 7
      can bind it to specific analysts if desired).
- [x] **Test**: `tests/test_rag_injection.py` — 11 tests covering
      threshold gating, seed building, `find_similar` since /
      exclude_run_ids, `count_embeddings`, and the tool's formatted
      output / ticker filter / since validation / empty-query.
- [x] **Smoke**: end-to-end seeded run against `pgvector/pgvector:pg18`
      shows the full three-section context: same-ticker NVDA history +
      cross-ticker AAPL + vector hits ordered by cosine distance with
      the deterministic seed match (cos=0.00) at the top.
- [x] **Regression**: 262 pass + 1 skip with DB, 224 pass + 39 skip
      without.

### Follow-ups (not part of Phase 5)

- **Manager tool binding**: the Research Manager + Portfolio Manager
  use `bind_structured` (single-shot structured output) — binding a
  tool would require interleaving a tool loop with structured output,
  a non-trivial refactor. The `search_past_analyses` tool exists and
  is exported via `@tool`; binding it to managers is a follow-up. The
  RAG context injection delivers the user's "compare to past findings"
  intent through the existing prompt-injection path that managers
  already consume.
- **Tool node buckets**: ditto — would need new entries in
  `TradingAgentsGraph._create_tool_nodes` for `"research"` and
  `"portfolio"` once managers grow tool loops.

## Phase 6 — Batches persistence ✅

Implemented as `tradingagents/persistence/batches.py` (DAO) +
`webui/batch.py` integration + hybrid recent-batches view in routes.
Per-ticker JSONB updates use `SELECT FOR UPDATE` to prevent races
between concurrent dispatcher workers.

- [x] **insert on start**: `BatchRegistry.start` calls
      `insert_batch(base_selections, tickers)` and stores the returned
      UUID on `BatchJob.db_batch_id`. Per-ticker `position` is assigned
      during normalization so the DAO knows which JSONB array slot to
      mutate.
- [x] **status transitions**: `update_batch_status` updates typed
      columns; `update_batch_ticker` mutates one entry in
      `payload["tickers"]` under a row lock. Called from every
      mutation site in `_run_batch` / `_absorb_completed` /
      `_run_ticker` / `cancel()`.
- [x] **batches list**: `_merged_recent_batches` hybrid view in
      `routes.py` (registry wins on `db_batch_id` collision, sorted by
      `submitted_at` DESC).
- [x] **DB fallback for batch detail**: `_load_batch_from_db`
      reconstructs a `RecentBatchRow` for `/batches/<id>` after
      restart. The row's `tickers` + `progress` properties keep the
      existing `partials/batch_status.html` template happy.
- [x] **restart behavior**: `BatchRegistry.__init__` calls
      `interrupt_in_flight_batches()` once per process — any
      queued/running/paused batches inherited from a prior process are
      flipped to `error` with `payload["interrupted_note"]` set, and
      their non-terminal tickers cancelled. (v2: real recovery by
      replaying the deque.)
- [x] **concurrent JSONB write safety**: `update_batch_ticker` uses
      `SELECT … FOR UPDATE` on the parent row. Tested with 8
      concurrent threads updating different positions — all 8 writes
      stick (no JSONB read-modify-write race).
- [x] **test**: `tests/test_batches.py` — 12 tests covering DAO
      operations, concurrent updates, restart recovery, and a
      BatchRegistry integration test that mocks the per-ticker
      JobRegistry to verify dispatcher mutations propagate to the DB.
- [x] **regression**: 274 pass + 1 skip with DB, 225 pass + 50 skip
      without. Zero regressions.

## Phase 7 — Settings + cleanup ✅

Implemented as `tradingagents/persistence/settings.py` DAO +
`webui/preferences.py` delegation. File-write deprecation is now
explicit in CLAUDE.md.

- [x] **webui_settings**: `load_last_settings` / `save_last_settings`
      now route through the `webui_settings` singleton row when DB is
      configured. The JSON file is neither read nor written in DB
      mode; any pre-existing file is left in place as a frozen
      artifact.
- [x] **remove dead writes**: already gated in Phase 2 — `_run`'s
      `if not persisted` branch only writes the JSON cache when the
      DB write failed/skipped. CLAUDE.md updated to call out the
      deprecation: both `webui_cache_*.json` and
      `webui_last_settings.json` are legacy fallbacks now.
- [x] **status_label parity**: already shipped in Phase 2 —
      `RecentRunRow.status_label` returns "Stop" for cancelled,
      mirroring `JobState.status_label`.
- [x] **test**: `tests/test_preferences_persistence.py` — 8 tests
      covering DAO upsert + idempotency, round-trip via DB,
      stale-file ignore when DB is on, file-only path when DB is off,
      PERSISTED_FIELDS filtering (junk keys dropped), empty-dict save.
- [x] **regression**: 282 pass + 1 skip with DB, 227 pass + 56 skip
      without. Zero regressions.

## Phase 8 — Docs + ops ✅

- [x] **README**: new "Postgres + pgvector (optional)" subsection under
      "Persistence and Recovery" covering schema, setup, embedding
      provider switching, backfill, multi-user concurrency, and
      restart recovery. News bullet added for v0.3.0.
- [x] **CLAUDE.md**: new "Postgres + pgvector persistence (optional,
      Phases 1–7)" subsection with the architecture diagram, key
      files table, DB-off fallback contract, and ops commands.
      Existing per-phase notes preserved.
- [x] **`.env.example`**: already done in Phase 1 — has
      `TRADINGAGENTS_DATABASE_URL` and the `TRADINGAGENTS_EMBEDDING_*`
      trio commented in with example values.
- [x] **CHANGELOG**: new `[0.3.0] — 2026-05-17` entry listing every
      addition, change, and operational note from Phases 1–7.
      `pyproject.toml` + `webui/app.py` version bumped.
- [x] **regression**: 282 pass + 1 skip with DB, 227 pass + 56 skip
      without. No regressions across the full 8-phase rollout.

---

## Risks tracker

- Concurrent updates to the `batches.payload` JSONB on rapid ticker
  state transitions could race. Mitigate with `SELECT FOR UPDATE` on
  the row before rewriting. Add to Phase 6.
- Postgres **18+** required (decision #8). HNSW + pgvector ≥ 0.5
  defaults are tuned for it. Pin `pgvector/pgvector:pg18` in compose;
  README documents the min server version. Verify the `pg18` image
  tag is published before merging Phase 1; if not, document the
  manual pin and a fall-forward path.
- OpenAI embeddings cost: budget < $0.001 per run; non-issue.
- Backfill is one-shot manual run, not part of startup. Acceptable —
  prevents repeated migrations on every restart.
- If we later need per-user filtering, all writes already have a
  `created_by text NULL` column placeholder (add to Phase 1 schema if
  not present — easier than a migration later). **Action**: add it to
  the Phase 1 schema task above.

---

## Out of scope reminders

- LangGraph checkpoint DBs stay on SQLite.
- No Grafana / dashboards.
- No CLI dashboard rework — CLI keeps writing its existing files;
  optionally also writes to DB if `TRADINGAGENTS_DATABASE_URL` is set.
  (Tracked as a follow-up; not required for v1.)
- No `full_states_log_*.json` import (per decision #3).
