# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install .                                # install package + CLI entry point
tradingagents                                # interactive CLI (alias for `analyze`, also: python -m cli.main)
tradingagents analyze --checkpoint           # enable LangGraph checkpoint/resume
tradingagents analyze --clear-checkpoints    # wipe per-ticker checkpoint DBs first
tradingagents batch NVDA AAPL MSFT           # non-interactive overnight batch (see batch section below)
tradingagents serve                          # HTMX web UI on http://127.0.0.1:8000
python main.py                               # programmatic single-shot run (edit ticker/date inline)

pytest                                       # run tests (pytest config in pyproject.toml; testpaths=tests)
pytest -m unit                               # markers: unit / integration / smoke
pytest tests/test_signal_processing.py -k name_of_test

# Provider smoke (real API calls — see scripts/smoke_structured_output.py docstring for all providers)
OPENAI_API_KEY=... python scripts/smoke_structured_output.py openai

docker compose run --rm tradingagents                              # containerized CLI (interactive)
docker compose run --rm --no-TTY tradingagents batch NVDA AAPL     # containerized batch (unattended)
docker compose --profile ollama run --rm tradingagents-ollama      # bundled Ollama + CLI
```

The Typer app has two subcommands: `analyze` (interactive, default when no subcommand given — preserved via an `invoke_without_command=True` callback so bare `tradingagents` and the Docker ENTRYPOINT still launch the dashboard) and `batch` (non-interactive multi-ticker driver). `batch` reads provider/model selection from `TRADINGAGENTS_*` env vars (no questionary prompts), enables `--checkpoint` by default, isolates per-ticker errors (`--fail-fast` to opt out), and writes the per-section markdown bundle to `<cwd>/reports/<TICKER>_<date>/` (`--no-save-reports` to skip). API keys must be present in the environment — missing keys exit with a clear message instead of prompting.

`conftest.py` autouses a `_dummy_api_keys` fixture that fills every provider env var with `"placeholder"` if unset, so tests that touch client construction don't blow up in CI. Tests that actually need a real key must override.

## Architecture

The system is a LangGraph state machine over a `TradingAgentsGraph` orchestrator (`tradingagents/graph/trading_graph.py`). One `propagate(ticker, trade_date)` call walks this fixed pipeline:

```text
Analyst chain (user-selected, sequential)
  Market → Sentiment → News → Fundamentals
    each: analyst LLM ⇄ tool_node ⇄ msg_clear, gated by ConditionalLogic.should_continue_*
→ Bull Researcher ⇄ Bear Researcher           (max_debate_rounds)
→ Research Manager                            (deep LLM, structured output)
→ Trader                                       (quick LLM, structured output)
→ Aggressive ⇄ Conservative ⇄ Neutral debaters (max_risk_discuss_rounds)
→ Portfolio Manager                           (deep LLM, structured output → final_trade_decision)
→ Hedging Agent                               (deep LLM, free-text → hedging_report; downside-protection strategy sized to observed weakness)
```

Edges and conditional routing live in `graph/setup.py` and `graph/conditional_logic.py`. The shared `AgentState` (`agents/utils/agent_states.py`) is a `MessagesState` plus per-report fields (`market_report`, `sentiment_report`, `news_report`, `fundamentals_report`, `investment_plan`, `trader_investment_plan`, `final_trade_decision`) and the nested `InvestDebateState` / `RiskDebateState` dicts. Adding a node means adding an entry to `AgentState`, a builder under `agents/`, and edges in `setup.py`.

Each analyst is paired with a `ToolNode` (`trading_graph._create_tool_nodes`) bound to abstract tool functions in `agents/utils/agent_utils.py`. Those abstract tools dispatch through `dataflows/interface.py`'s `VENDOR_METHODS` table — `config["data_vendors"]` sets the category-wide vendor (yfinance/alpha_vantage), and `config["tool_vendors"]` overrides at the per-tool level. Add a vendor by registering its implementations in `VENDOR_METHODS` and listing it in `VENDOR_LIST`.

### LLM provider layer

Provider-specific glue lives in `tradingagents/llm_clients/`. `create_llm_client(provider, model, base_url, **kwargs)` (factory.py) returns one of four clients; all OpenAI-compatible providers (`openai`, `xai`, `deepseek`, `qwen`, `qwen-cn`, `glm`, `glm-cn`, `minimax`, `minimax-cn`, `ollama`, `openrouter`) share `OpenAIClient`. `anthropic`, `google`, and `azure` have dedicated clients. Provider-specific reasoning/thinking kwargs (`openai_reasoning_effort`, `anthropic_effort`, `google_thinking_level`) are funnelled through `TradingAgentsGraph._get_provider_kwargs`. When adding a provider:

1. Add the env-var to `llm_clients/api_key_env.py` so the CLI prompt picks it up.
2. If OpenAI-compatible, list it in `factory._OPENAI_COMPATIBLE`; otherwise add a new client class.
3. Register model choices in `llm_clients/model_catalog.py` (`MODEL_OPTIONS[provider]["quick"|"deep"]`). Always include a `"Custom model ID"` row.
4. If the provider supports structured output via something other than json_schema/tool-use, wire it in `agents/utils/structured.py`.

### Configuration

`tradingagents/default_config.py` is the single source of truth. `_apply_env_overrides` reads `TRADINGAGENTS_*` env vars at module load and coerces them to the type of the existing default — so anything in the `_ENV_OVERRIDES` table can be set from `.env` without code changes (provider, deep/quick models, backend URL, output language, debate/risk rounds, checkpoint flag, benchmark ticker, plus provider-specific `openai_reasoning_effort` / `anthropic_effort` / `google_thinking_level`). For keys whose default is `None`, `_coerce` returns the env value verbatim — the receiving client validates the actual value. To make a new config key env-overridable, add one line to `_ENV_OVERRIDES`. The CLI also persists API keys to `.env` interactively when missing (see `cli/utils.py::ensure_api_key`).

`tradingagents/dataflows/config.py` holds a module-global mirror of the config used by tool functions. `TradingAgentsGraph.__init__` calls `set_config(self.config)` to push the user-supplied config into that mirror; dict-valued keys merge one level deep, scalars replace. Never reach for the global directly from new code — go through `get_config()`.

### Persistence

Two independent always-on layers + an optional Postgres layer:

- **Decision log** (`TradingMemoryLog`). Markdown at `~/.tradingagents/memory/trading_memory.md` when DB is off; delegates to the `decisions` table when on (Phase 3). On the next same-ticker run, `_resolve_pending_entries` fetches realized return vs the resolved benchmark (`_resolve_benchmark` picks per-suffix from `benchmark_map` — `^N225` for `.T`, `^FTSE` for `.L`, SPY default — unless `benchmark_ticker` overrides), generates a reflection via `Reflector`, and rewrites the entry as `resolved` in a single atomic batch. `get_past_context()` injects same-ticker decisions + cross-ticker lessons + (DB-only) vector-retrieved similar past summaries into the Portfolio Manager prompt.
- **Checkpoint/resume** (opt-in, `config["checkpoint_enabled"]` or `--checkpoint`). LangGraph SqliteSaver per ticker at `~/.tradingagents/cache/checkpoints/<TICKER>.db`. Thread ID is `ticker+date`, so same-ticker-same-date resumes, different date starts fresh. Auto-cleared on successful completion; reset all with `--clear-checkpoints`.
- **Postgres + pgvector** (opt-in via `TRADINGAGENTS_DATABASE_URL`). See the next section for the full architecture.

### Postgres + pgvector persistence (optional, Phases 1–7)

The persistence package at `tradingagents/persistence/` adds a SQLAlchemy 2.x ORM + Alembic migrations + a pgvector vector store. **Strictly opt-in** — when `TRADINGAGENTS_DATABASE_URL` is unset, every persistence call site falls back to the legacy in-memory + file-based behavior. When set, the DB becomes the canonical store and file-based mirrors (markdown decision log, `webui_cache_*.json`, `webui_last_settings.json`) stop being written.

**Architecture at a glance:**

```
                                  ┌─────────────────────────────────┐
                                  │   tradingagents/persistence/    │
                                  │                                  │
  webui.runner.JobRegistry ──┐    │  runs / run_reports /            │
      (in-memory live state) │    │  run_token_usage   (Phase 2)     │
                             ├───→│                                  │
  webui.batch.BatchRegistry ─┘    │  batches    (Phase 6, FOR UPDATE)│
      (in-memory live state)      │                                  │
                                  │  decisions  (Phase 3, sunsets    │
  TradingMemoryLog ────────────→  │     trading_memory.md)           │
      (markdown adapter)          │                                  │
                                  │  run_embeddings (Phase 4, pgvector│
  EmbeddingService  ─────────→    │     HNSW cosine index)           │
      (openai/google route)       │                                  │
                                  │  webui_settings (Phase 7,        │
  webui.preferences ────────────→ │     sunsets _last_settings.json) │
                                  └─────────────────────────────────┘
                                              │
                                              ▼
                                   PostgreSQL ≥ 18 + pgvector
                                   (docker-compose `persistence` profile)
```

**Key files:**

| Module | Purpose |
| --- | --- |
| `engine.py` | Process-singleton engine + session factory. Returns `None` when no URL configured. Reads `database_url` via `_current_db_url()` each call (immune to module reloads). |
| `session.py` | `session_scope()` context manager — yields `Session` or `None`, commits on success, rolls back on exception. |
| `models.py` | SQLAlchemy 2.x ORM: `Run`, `RunReport`, `RunTokenUsage`, `Decision`, `Batch`, `RunEmbedding`, `WebUISettings`. JSONB-heavy per decision #6; typed columns only for filtered/indexed fields. |
| `runs.py` | Run lifecycle (Phase 2): `insert_run`, `persist_run_completion`, `find_cache_hit_db`, `list_recent_terminal_runs`. `RecentRunRow` mirrors `JobState.status_label` for templates. |
| `decisions.py` | Decision log replacement (Phase 3): `store_pending_decision`, `resolve_decisions`, `get_past_context_db` with optional RAG augmentation. |
| `embeddings.py` | Vector store (Phase 4–5): `EmbeddingService` (auto-routes openai/google), `persist_embeddings_for_run`, `find_similar` (cosine + HNSW), `count_embeddings`. |
| `batches.py` | Batches DAO (Phase 6): `update_batch_ticker` uses `SELECT FOR UPDATE` on the row before mutating its JSONB tickers array — safe under the dispatcher's `BATCH_MAX_PARALLEL=3` concurrency. `interrupt_in_flight_batches` runs on `BatchRegistry.__init__` for restart recovery. |
| `settings.py` | Singleton row DAO (Phase 7): `load_settings_db`, `save_settings_db`. The `CHECK(id=1)` constraint enforces the single-row invariant at the DB level. |
| `schedules.py` | Schedules DAO: `upsert_schedule_db`, `delete_schedule_db`, `list_schedules_db`. Write-through mirror for `webui.schedules.ScheduleRegistry` — the registry loads everything at startup and is authoritative in-memory; `list_schedules_db` returns `None` (vs `[]`) on no-DB so the store knows to fall back to the JSON file. |
| `migrations/versions/` | Alembic migrations: `0001_initial.py` (all 7 tables + pgvector extension + HNSW index), `0002_runs_company_name.py`, `0003_schedules.py`. Driven by `python -m tradingagents.persistence upgrade`. |
| `uuid7.py` | `uuid7()` helper (RFC 9562) used as the Python-side `default` for `runs.id` and `batches.id` — time-ordered UUIDs keep PK B-tree inserts append-only. Resolution order: stdlib `uuid.uuid7` (3.14+) → `uuid_utils.uuid7` (transitive via langchain-core) → native fallback. Returns a stdlib `uuid.UUID`. |

**DB-off fallback contract** (the single most important property of the package):

Every persistence call site is wrapped: when `session_scope()` yields `None` or any DB call raises, the function logs a warning and returns a sentinel (None / False / 0 / []). The caller never sees an exception from persistence. This means a DB outage during a run degrades to "this run didn't persist" — the run itself completes, the user gets their result, the RAG context is just empty for that one analysis. Mirrors of file-based state (markdown decision log, JSON cache, JSON settings) are written **only** when the DB write returned False — DB success is treated as the canonical store and the file is silently skipped.

**Provider for embeddings is deployment-level, not per-run.** pgvector columns have a fixed width; mixing 1536-dim (OpenAI) and 768-dim (Google) vectors in the same column would fail. `EmbeddingService` resolves the provider once at construction: explicit `embedding_provider` config wins; otherwise follows the main `llm_provider` if it's openai/google; otherwise falls back to openai (with a logged note that the user needs `OPENAI_API_KEY` set even though their main pipeline uses Anthropic / xAI / etc.). Switching providers requires `scripts/reembed.py`.

**Migrations / ops:**

```bash
python -m tradingagents.persistence upgrade        # apply all migrations
python -m tradingagents.persistence current        # show current revision
python -m tradingagents.persistence downgrade -1   # roll back one revision
python scripts/backfill_decisions.py               # one-shot markdown → decisions
python scripts/reembed.py                          # re-embed all `done` runs
```

### CLI

`cli/main.py` is a Typer app with a Rich-based live dashboard. `MessageBuffer` tracks per-agent status and report sections; `update_analyst_statuses` parses LangGraph stream chunks to detect which agent just finished. The CLI selects providers via `cli/utils.py` prompts, region-splits Qwen/GLM/MiniMax into international vs China endpoints, and renders the resolved Ollama endpoint when applicable. **`safe_ticker_component()` (`dataflows/utils.py`) hardens the ticker against path traversal** — use it whenever building filesystem paths from a ticker.

### Web UI (`webui/`)

`tradingagents serve` launches a FastAPI + HTMX UI that mirrors the CLI's selection flow in the browser and polls for live progress while the graph runs. Architecture:

- **`webui/runner.py`** holds a process-global `JobRegistry` that supports **multiple concurrent jobs**. Each job runs in its own daemon thread; isolation is provided by `tradingagents.dataflows.config._active_config` being a `ContextVar` (per-thread, replaces the prior module-global), so concurrent jobs see their own provider / vendor / output-language settings without racing. `start()` spins a daemon thread that calls `TradingAgentsGraph.propagate()` directly, capturing per-agent progress via a `_ProgressCallback` and token/call stats via `StatsCallbackHandler`. Final state lands in `JobState.final_state`.
- **`webui/cache.py`** persists each completed run to `<results_dir>/<safe_ticker>/webui_cache_<analysis_date>.json` (sibling to the existing `TradingAgentsStrategy_logs/`). `JobRegistry.start()` checks this on submission: if a cache file for the requested `(ticker, date)` exists with mtime ≤ 7 days, it's loaded straight into `JobState` (status `done`) and the LangGraph pipeline is skipped — the user gets instant results without burning API tokens. `RunSelections.force_refresh` (a checkbox on the form) bypasses the check. The same `json_safe` helper is shared with `routes.py::export_job_json`. **When the DB is configured (Phase 2+), the JSON cache file is no longer written** — `runs` + `run_reports` + `run_token_usage` take over the cache role and the file becomes a legacy fallback used only when `TRADINGAGENTS_DATABASE_URL` is unset. Same goes for `webui_last_settings.json` (Phase 7): replaced by the singleton `webui_settings` row when DB is on; pre-existing JSON files are left as frozen artifacts.
- **Cooperative cancellation**: `JobRegistry.cancel(job_id)` flips `JobState.cancel_requested`; `_ProgressCallback` checks the flag on every `on_chain_start` / `on_llm_start` / `on_chat_model_start` / `on_tool_start` and raises `JobCancelled` (a subclass of `Exception` defined in the same module). LangGraph propagates the exception out of `propagate()`; `_run` catches it and sets status to `cancelled` (a distinct terminal state from `error`). The cache is NOT saved on cancellation. Worst-case unwind latency is one in-flight LLM call. The UI's Stop button hits `POST /api/jobs/<id>/cancel` and gets back an immediately re-rendered status fragment so the button greys out before the next 2 s poll.
- **`webui/batch.py`** layers batch submission on top of the per-ticker `JobRegistry`. `BatchRegistry.start(base_selections, tickers)` deduplicates the ticker list, deep-copies the base `RunSelections` per ticker, and runs the batch in a daemon thread driven by `ThreadPoolExecutor(max_workers=BATCH_MAX_PARALLEL=3)`. Each ticker spawns a normal `JobState` (so `/jobs/<id>`, the cache, and per-ticker cancellation all work), and the dispatcher polls the inner job for terminal status. **Rate-limit handling**: `_is_rate_limit_error` heuristically matches signals like `RateLimitError`, ` 429`, `too many requests`, `quota exceeded`, etc. across provider SDKs; on hit the batch is marked `paused`, `paused_until` is armed for `BATCH_RATE_LIMIT_PAUSE` (2 h), in-flight workers are drained, and the rate-limited ticker is re-queued to the front so it's the first to resume. Cancellation flows down to each in-flight per-ticker `JobRegistry.cancel(...)`. Batch UI: `GET /batch` (submission form), `GET /batches/<id>` (detail), `GET /htmx/batches/<id>/status` (polled every 2 s, morph swap so the ticker table doesn't blink), `POST /api/batches/<id>/cancel`.
- **`webui/schedules.py`** adds recurring unattended analyses on top of `BatchRegistry`. A `Schedule` (Pydantic) couples a ticker list + base `RunSelections` with a cadence (`daily` / `weekdays` / `weekly` at a server-local `HH:MM`; weekly adds a weekday). `ScheduleRegistry` holds all schedules in memory and runs a daemon ticker thread (started from the FastAPI lifespan — NOT module import, so tests constructing the app don't spawn it) that wakes every 30 s and fires due schedules via `get_batch_registry().start(...)` with `analysis_date` = fire date. **Catch-up semantics**: the due check is `next_run_at <= now` against the *persisted* `next_run_at`, so a schedule missed during downtime fires once at startup, then advances — at most one catch-up batch regardless of downtime length. Re-enabling a paused schedule recomputes `next_run_at` from now (no instant catch-up). Fire failures (missing API key, batch submission error) land in `schedule.last_error` and still advance the slot so a broken schedule can't retry every tick. Storage is write-through via `ScheduleStore`: `schedules` table when DB is on, else atomic-rewrite JSON at `~/.tradingagents/webui_schedules.json`. UI: `GET /schedules` (form + table, polled every 30 s), `POST /api/schedules`, per-schedule `run` / `toggle` / `delete` actions.
- **`webui/routes.py`** has three groups: page routes (`/`, `/jobs/<id>`), HTMX fragment routes (`/htmx/models`, `/htmx/effort`, `/htmx/jobs/<id>/status` — polled every 2 s by the job page), and the API export (`/api/jobs/<id>/export.json`). The index's History panel groups rows one-line-per-ticker (`_group_history`; expandable via the `group-row`/`group-child` JS hook in base.html) — Recent jobs stays flat.
- **Provider→model dependency** is wired via HTMX: changing the provider `<select>` triggers `GET /htmx/models` and `GET /htmx/effort`, swapping in the right model dropdowns and the right effort/thinking knob.
- **Polling stops on terminal state** via a small inline script in `partials/job_status.html` that strips the parent's `hx-trigger` once status is `done` or `error` — otherwise the tab strip would re-render every 2 s and reset the user's active tab.

**Important coupling**: `TradingAgentsGraph._run_graph` now threads `self.callbacks` to *both* the LLM clients (via `__init__`) **and** the graph chain (via `Propagator.get_graph_args(callbacks=...)`). Without the chain-level binding the web UI's `_ProgressCallback` would never see `on_chain_start`/`on_chain_end` for node transitions. The change is additive — handlers like `StatsCallbackHandler` that only implement LLM hooks simply ignore the new chain events. If you add a new callback handler, decide whether it should care about node events: filter by `metadata["langgraph_node"]` and check against the friendly-name map in `webui/runner.py::NODE_LABELS` (mirrors the strings used in `graph/setup.py::workflow.add_node(...)`).

### Where output lands

A completed run writes to three independent locations:

1. **Live streaming log** (always, created before the graph starts). `cli/main.py:1004-1009`:
   `<results_dir>/<ticker>/<analysis_date>/message_tool.log` — every agent message and tool call, appended as it streams. `results_dir` defaults to `~/.tradingagents/logs/` (override with `TRADINGAGENTS_RESULTS_DIR`).
2. **Full-state JSON** (always, on successful completion). `TradingAgentsGraph._log_state` (`graph/trading_graph.py:418-423`):
   `<results_dir>/<safe_ticker>/TradingAgentsStrategy_logs/full_states_log_<trade_date>.json` — the entire `AgentState` serialized. Note: sibling to #1 under the same ticker folder, *not* the same path.
3. **Structured per-section dump** (only if user answers Y to "Save report?"). `save_report_to_disk` (`cli/main.py:673`):
   Default `<cwd>/reports/<TICKER>_<YYYYMMDD_HHMMSS>/` with numbered subfolders `1_analysts/`, `2_research/`, `3_trading/`, `4_risk/`, `5_portfolio/` plus `complete_report.md`. User can override the path at the prompt — this one is launch-directory-relative, not under `results_dir`.

Plus the always-on decision log appends a `pending` entry to `~/.tradingagents/memory/trading_memory.md` (overridable via `TRADINGAGENTS_MEMORY_LOG_PATH`).

## Conventions worth knowing

- The selector key `"social"` is preserved in `selected_analysts` for back-compat; the underlying agent is now `sentiment_analyst` (renamed in v0.2.5 — it never had social-media access before that). `create_social_media_analyst` is a deprecated alias.
- Internal agent debate stays in English regardless of `output_language` (for reasoning quality); only user-facing report text is localized. `get_language_instruction()` in `agent_utils.py` returns the empty string for English so no tokens are wasted.
- Structured-output decision agents (Research Manager, Trader, Portfolio Manager) bind Pydantic schemas from `agents/schemas.py` via `agents/utils/structured.py`. The provider's native mode is used (json_schema for OpenAI-family, response_schema for Gemini, tool-use for Anthropic) — see `scripts/smoke_structured_output.py` to verify a provider end-to-end.
- Python ≥3.10 required (pyproject); README installation snippet uses 3.13, Dockerfile builds on 3.12 — anything in that range works.
