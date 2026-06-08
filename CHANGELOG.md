# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [Unreleased]

### Added

- **Scheduled analyses** (`/schedules` in the web UI). Define a ticker
  list + the usual selection controls + a cadence (daily, weekdays, or
  weekly at a server-local time of day) and the server runs it
  unattended: a daemon ticker thread checks every 30 s and fires due
  schedules as ordinary batches (3-way parallelism, rate-limit pause,
  per-ticker jobs in Recent jobs / History), using the fire date as the
  analysis date. Schedules survive restarts — stored in the new
  `schedules` table when `TRADINGAGENTS_DATABASE_URL` is set (Alembic
  migration `0003`), else in `~/.tradingagents/webui_schedules.json` —
  and a run that was due during downtime is caught up once at startup.
  Per-schedule actions: run now (extra run, cadence unchanged),
  pause/resume (resume recomputes the next slot — no surprise
  catch-up fire), delete. Fire-time failures (missing API key, batch
  submission error) are recorded on the schedule row and never spin
  the loop.
- **History grouped by ticker.** The History panel on the index page
  now shows one expandable line per ticker (ticker, company name,
  analysis count, latest run, latest status/decision); clicking the
  line unfolds the full per-analysis table for that symbol. Repeated
  scheduled runs of the same watchlist no longer flood the table with
  near-identical rows.

### Added

- **Traffic-light assessment dots** on the analysis page. Each agent tab
  (and its panel heading) now shows a green / amber / red dot summarizing
  how constructive that section is about the stock. The synthesis agents'
  explicit ratings are parsed directly (Research Manager / Portfolio
  Manager 5-tier → Buy/Overweight green, Hold amber, Underweight/Sell
  red; Trader action; Hedging Agent weakness level inverted); free-text
  analyst/debate sections are scored with a small financial lexicon.
  Deliberately pessimism-biased: a mixed or ambiguous read resolves to
  amber and a slight bearish lean to red, so green requires a clear
  constructive majority (`webui/assessment.py`).
- **Hedging Agent.** A new final pipeline node (deep LLM) that runs
  after the Portfolio Manager, reads every upstream output (analyst
  reports, research plan, trader proposal, full risk debate, final
  decision), rates overall weakness LOW/MODERATE/ELEVATED/SEVERE from
  the cited evidence, and designs a downside-protection strategy sized
  to that rating (no hedge when LOW; stops/trims/OTM puts → puts/collars/
  index-sector hedges → capital preservation as weakness rises), with
  concrete legs, costs, triggers, unwind conditions, and residual-risk
  notes. Surfaced as a "Hedging Agent" tab in the web analysis window,
  a section in the CLI report + saved bundle (`6_hedging/`), and an
  input to the Summary. New `hedging_report` state field.
- **newsdata.io as a news vendor** (`get_global_news` only, via the
  official `newsdataapi` client and the finance-scoped `/api/1/market`
  endpoint — note it rejects the `category` parameter). Now the default
  vendor for macro headlines (`tool_vendors: {"get_global_news":
  "newsdata"}`): proper query/country/language filtering gives far
  better relevance than yfinance's fuzzy Search. Requires
  `NEWSDATA_API_KEY` in `.env`; when the key is missing the
  implementation logs a warning and falls back to yfinance
  automatically. Request filters live in `newsdata_params`
  (`country: us,gb` default, `country_canada: ca,us` for Canadian
  listings). Rate-limit handling: low retry counts (the client handles
  Retry-After/backoff), and a 429 stops the remaining queries instead
  of stalling the analyst tool call.

### Changed

- **Dedicated `/healthz` liveness probe + quieter access log.** The
  Docker healthcheck now hits `/healthz` (a cheap no-dependency 200)
  instead of `/favicon.ico`, and a uvicorn access-log filter suppresses
  the log line for both `/healthz` and `/favicon.ico` so the 10 s health
  poll no longer floods the container log.

### Fixed

- **"Open" in the jobs/batch tables 404'd (`Unknown job id`) after a
  server restart** until the page was refreshed. The links and the
  `/jobs/{id}` + `/htmx/jobs/{id}/status` routes keyed off the in-memory
  `JobState.id`, which is ephemeral — once the registry was cleared by a
  restart, the stale link resolved against neither the registry nor the
  DB (which is keyed by `runs.id`). Links now prefer the durable
  `db_run_id` (`runs.id`), the routes resolve registry-by-id →
  registry-by-`db_run_id` (keeps live polling) → DB-by-`runs.id`, and
  the same durable id is threaded through batch per-ticker rows. Run
  status is now sourced from the DB when the session no longer has it.
- **Orphaned `running` runs after a restart.** A run left `queued`/
  `running` when the process died would sit at `running` forever (its
  owning `JobState` is gone) and the detail page would poll it
  indefinitely. `interrupt_in_flight_runs()` now reconciles them to
  `error` on web startup (mirrors the existing batch recovery), so the
  UI shows a terminal status from the DB.
- **500 on the index page when a batch mixed cached and freshly-run
  tickers.** `JobRegistry.list_jobs` sorted `JobState`s by `started_at`,
  but cache-hydrated jobs carry tz-aware UTC timestamps while live runs
  used naive `datetime.now()` — comparing the two raised
  `TypeError: can't compare offset-naive and offset-aware datetimes`.
  Sorting now reduces to epoch seconds (tz-safe), and all job timestamps
  are stamped aware-UTC so the two paths can't diverge again.

### Changed

- **Global news is ticker-aware and actually consults the query list.**
  `get_global_news` now takes the analyzed ticker: Canadian listings
  (`.TO` / `.V` / `.CN` / `.NE` — every Canadian exchange Yahoo
  qualifies: TSX, TSX Venture, CSE, Cboe Canada/NEO) use the new
  Canada-focused
  `global_news_queries_canada` set, everything else the default macro
  set. The fetch loop takes up to `global_news_articles_per_query`
  (default 2) fresh articles per query in priority order until
  `global_news_article_limit` — previously the first query filled the
  entire quota and the other ~50 configured queries never executed.
  Shared routing logic lives in `dataflows/news_queries.py`. Also fixed
  a latent crash in the Alpha Vantage global-news path when the tool's
  `None` defaults reached `timedelta(days=None)`.
- **newsdata.io rate limits now fail fast.** The vendor constructs the
  client with `max_retries=1`, so a 429 raises immediately instead of
  sleeping the server's `Retry-After` (often several minutes on the
  free tier) before a pointless retry — a rate-limited macro-news call
  no longer freezes an analyst tool node for minutes mid-run.
- **Agent prompts overhauled for modern LLM guidance** (all roles, format
  contracts unchanged). Removed the vestigial multi-assistant "swarm"
  scaffold from the five analysts — single coherent role statement, and
  analysts are now explicitly told *not* to emit a buy/sell/hold call
  (that decision belongs downstream; the rendered
  `FINAL TRANSACTION PROPOSAL` line from the Trader's structured output
  is unchanged). All injected reports, debate histories, and plans are
  wrapped in XML tags with a data-not-instructions guard wherever
  external text (news, social posts) enters a prompt. Analysts gained
  explicit tool-calling processes and when-to-call triggers plus
  concrete report specs (incl. invalidation conditions) in place of
  "very detailed and nuanced". Debaters (bull/bear + the three risk
  analysts) gained debate rules: steelman the strongest opposing point
  first, no cross-round repetition, every claim cites a figure from the
  reports, hold the assigned stance with narrow honest concessions;
  finder roles (bear, conservative) carry an explicit coverage mandate
  while the judges (Research Manager, Portfolio Manager) gained a
  judging rubric — weigh evidence over eloquence/recency, filter
  low-confidence points — and must state invalidation/revisit triggers.
  The Trader is re-anchored as an execution role (direction, conviction,
  entry, sizing, stop, horizon — no re-litigating research), the Neutral
  risk analyst has a distinct job (quantify risk/reward asymmetry,
  propose the sizing/hedging middle path), and the Reflector gained a
  luck-vs-skill guard so noisy short-window alpha doesn't write spurious
  lessons into the decision log.
- **Fundamentals analyst fixes**: the system prompt was accidentally a
  1-tuple (trailing comma) and rendered as a Python `repr` — now a plain
  string; `get_insider_transactions` is now actually bound to the
  fundamentals analyst (tool list + ToolNode) and referenced in its
  prompt.
- **UUIDv7 primary keys** for `runs` and `batches`. The Python-side
  `default` for both PK columns is now `tradingagents.persistence.uuid7.uuid7`
  (RFC 9562) instead of `uuid.uuid4`, so newly-inserted rows are
  time-ordered and append to the B-tree index without page splits.
  The column type (`UUID`) is unchanged — no migration needed; existing
  v4 rows coexist with new v7 rows. The helper prefers stdlib
  `uuid.uuid7` (Python 3.14+), falls back to `uuid_utils.uuid7` (already
  transitive via langchain-core), then a native implementation.

## [0.3.0] — 2026-05-17

### Added

- **Postgres + pgvector persistence** (opt-in via
  `TRADINGAGENTS_DATABASE_URL`). New
  `tradingagents/persistence/` package with SQLAlchemy 2.x ORM,
  Alembic migrations, and a vector store. When configured, runs +
  reports + token usage + decisions + batches + form preferences move
  from file-based stores to Postgres; when unset, the app behaves
  exactly as before (in-memory + markdown decision log + JSON cache).
- **Vector store + RAG injection.** Each successful run embeds
  `summary` / `final_trade_decision` / `investment_plan` through the
  configured embedding provider (OpenAI `text-embedding-3-small` by
  default; Google `text-embedding-004` supported) and stores them in
  `run_embeddings` with an HNSW cosine index. The Portfolio Manager's
  `past_context` is automatically augmented with top-K semantically
  similar past analyses (any ticker — industry context) once the
  store has ≥ 5 embedded runs.
- **`search_past_analyses` LangChain tool** — exposed for any agent
  with a tool loop to query the vector store for past analyses
  matching a free-text query, optionally filtered by ticker or
  `since` date.
- **Multi-user concurrency.** `webui/runner.py` now supports
  concurrent analyses — each runs in its own daemon thread with a
  `ContextVar`-scoped dataflows config so parallel jobs don't trample
  each other's provider / vendor / output-language settings. The
  single-job lock is gone.
- **Cooperative cancel** via `JobRegistry.cancel(...)` —
  `_ProgressCallback` checks the flag on every `on_chain_start` /
  `on_llm_start` / `on_chat_model_start` / `on_tool_start` and raises
  `JobCancelled`. The Stop button in the web UI surfaces this with a
  red outline and "Stopping…" disabled-state during the unwind.
- **Cache hits short-circuit the pipeline.** `JobRegistry.start`
  checks Postgres for a run with the same `(ticker, analysis_date)`
  within the last 7 days; on hit, the cached `final_state` +
  `run_reports` populate the new `JobState` directly and `propagate`
  is skipped. JSON file cache remains the fallback when DB is off.
- **Batches persistence + restart recovery.** Batch dispatcher state
  is mirrored to Postgres on every transition; per-ticker JSONB
  updates use `SELECT FOR UPDATE` for safe concurrent writes from
  the dispatcher's worker pool. On server restart, any in-flight
  batches are marked `error` with a payload note explaining the
  interruption.
- **Hybrid Recent jobs / Recent batches views.** In-memory
  `JobRegistry` / `BatchRegistry` are the source of truth for
  in-flight rows; Postgres is canonical for terminals. Deduped by
  `db_run_id` / `db_batch_id` (registry wins on collision).
- **`scripts/backfill_decisions.py`** — one-shot import of
  `~/.tradingagents/memory/trading_memory.md` into the `decisions`
  table. Idempotent on `UNIQUE(ticker, trade_date)`.
- **`scripts/reembed.py`** — wipe + re-embed every `done` run through
  the currently-configured embedding provider. Documented as the
  migration path when switching embedding model / dim.
- **docker-compose `persistence` profile** — runs
  `pgvector/pgvector:pg18` as the `postgres` service with a
  healthcheck. The `tradingagents` service automatically picks up
  `TRADINGAGENTS_DATABASE_URL` from the profile.

### Changed

- **`TradingMemoryLog` delegates to DB when configured** — markdown
  writes are sunset under DB mode; existing `trading_memory.md` files
  are left in place as frozen historical artifacts.
- **`webui_last_settings.json` and `webui_cache_*.json` are sunset
  under DB mode** — the `webui_settings` and `runs` + `run_reports`
  tables take over. File mirrors remain the fallback when DB is off.
- **`engine.py` reads `database_url` per-call** instead of capturing
  it at module import, so `importlib.reload(default_config)` (used
  in tests) doesn't leave the engine pointing at a stale config dict.

### Notes

- New deps: `sqlalchemy>=2.0`, `psycopg[binary]>=3.2`,
  `alembic>=1.13`, `pgvector>=0.3`.
- Min Postgres version: 18 (for HNSW + recent pgvector defaults).
  Compose pins `pgvector/pgvector:pg18`.
- Decision-log markdown sunset is **non-breaking** — the file is
  not deleted; an explicit backfill script imports it into Postgres.
  The DB-off fallback keeps working for operators who don't want to
  run Postgres.
- Manager tool-binding (binding `search_past_analyses` to Research
  Manager / Portfolio Manager LLMs) is deferred — those managers use
  `bind_structured` for single-shot structured output, and adding a
  tool loop is a larger refactor. The RAG context-injection path
  delivers the same "compare to past findings" capability through
  the existing prompt-injection path.

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
