<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="./assets/wechat.png" target="_blank"><img alt="WeChat" src="https://img.shields.io/badge/WeChat-TauricResearch-brightgreen?logo=wechat&logoColor=white"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <br>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/Join_GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>

<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: Multi-Agents LLM Financial Trading Framework

## News
- [2026-05] **TradingAgents v0.3.0** released with optional **Postgres + pgvector persistence**: full run history, vector-store RAG injection into the Portfolio Manager prompt, the `search_past_analyses` tool, multi-user concurrency, cooperative cancellation, batch / cache / settings persistence, and restart recovery. Strictly opt-in (`TRADINGAGENTS_DATABASE_URL`). See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

<div align="center">
<a href="https://www.star-history.com/#TauricResearch/TradingAgents&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" />
   <img alt="TradingAgents Star History" src="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" style="width: 80%; height: auto;" />
 </picture>
</a>
</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

<div align="center">

🚀 [TradingAgents](#tradingagents-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles. This ensures the system achieves a robust, scalable approach to market analysis and decision-making.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions. It determines the timing and magnitude of trades based on comprehensive market insights.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone TradingAgents:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

One-command bring-up — starts Postgres + applies migrations + serves the web UI on http://localhost:8000:

```bash
cp .env.example .env       # fill in at least one provider API key
docker compose up -d
# → http://localhost:8000
docker compose logs -f web # tail the server log
docker compose down        # stop (keeps the volumes — state persists)
docker compose down -v     # nuke volumes too
```

The default `up` brings up three services: `postgres` (pgvector/pgvector:pg18) with a healthcheck, `migrate` (one-shot — runs `python -m tradingagents.persistence upgrade` then exits), and `web` (FastAPI + HTMX UI on port 8000). The web service `depends_on` the migrator completing successfully, so the schema is always at head before traffic hits the server.

**Interactive CLI** (questionary-driven flow) under an opt-in profile:

```bash
docker compose --profile cli run --rm cli analyze              # interactive
docker compose --profile cli run --rm --no-TTY cli batch NVDA AAPL MSFT
```

**Local LLMs via Ollama** under its own profile:

```bash
docker compose --profile ollama up -d ollama
# Set OLLAMA_BASE_URL=http://ollama:11434/v1 in .env so the web service
# can reach it via the compose network. Then pick "Ollama" in the form.
```

**Disabling Postgres** for a quick local test — set `TRADINGAGENTS_DATABASE_URL=` (empty) in `.env`. The app falls back to in-memory state + markdown decision log + JSON cache files. Postgres still starts but is unused (you can stop it with `docker compose stop postgres`).

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen — International (dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen — China (dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax — Global (api.minimax.io, M2.x, 204K ctx)
export MINIMAX_CN_API_KEY=...      # MiniMax — China (api.minimaxi.com, M2.x, 204K ctx)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For enterprise providers (e.g. Azure OpenAI, AWS Bedrock), copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more.

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Web UI

For a browser-driven workflow, launch the HTMX-based web UI:

```bash
tradingagents serve                              # http://127.0.0.1:8000
tradingagents serve --host 0.0.0.0 --port 8080   # LAN access
```

The UI mirrors the interactive CLI's selection flow — ticker, date, analysts, provider/model pickers (with dependent dropdowns), research depth, output language, and provider-specific effort/thinking — then renders a polling status block (current agent, progress bar, completed-agent list) while the analysis runs. Once complete, each agent's report appears as its own tab; the **Export** tab downloads the full pipeline state (selections, decision, stats, every report and debate history) as a single JSON file.

Defaults are read from `TRADINGAGENTS_*` env vars / `.env`, so a configured provider/model pair is preselected on page load. API keys must be present in the environment — missing keys produce an inline error instead of prompting. Only one analysis runs at a time (the dataflows config mirror is process-global).

### Batch mode (non-interactive)

For unattended overnight runs across multiple tickers, use the `batch` subcommand. It bypasses every interactive prompt — provider, models, debate depth, and effort levels all come from `TRADINGAGENTS_*` env vars (see [`.env.example`](.env.example)), so the same configuration drives every ticker in the batch.

```bash
tradingagents batch NVDA AAPL MSFT GOOGL META
tradingagents batch NVDA AAPL --date 2026-05-15 --research-depth 2
tradingagents batch NVDA TSM.NS 7203.T --analysts market,news,fundamentals --fail-fast
```

Flags:

| Flag | Default | Effect |
| --- | --- | --- |
| `--date YYYY-MM-DD` | today | Analysis date for every ticker in the run. |
| `--analysts <csv>` | `market,social,news,fundamentals` | Subset of analyst keys (comma-separated). |
| `--research-depth N` | `1` | Max rounds for both the researcher and risk debates. |
| `--checkpoint / --no-checkpoint` | on | Resume from last completed node on crash. Default on for batch. |
| `--save-reports / --no-save-reports` | on | Write the per-section markdown bundle to `<cwd>/reports/<TICKER>_<date>/`. |
| `--fail-fast` | off | Abort on the first ticker failure (default: continue and report at the end). |

The chosen provider's API key must already be in the environment — batch mode does not prompt for missing keys. A summary table is printed at the end with each ticker's status, decision, LLM call count, and token totals. Exit code is `0` if every ticker succeeded, `1` otherwise.

#### Configuring batch runs via `.env`

Set `TRADINGAGENTS_*` keys in `.env` once; every batch picks them up automatically:

```env
OPENAI_API_KEY=sk-...
TRADINGAGENTS_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.4
TRADINGAGENTS_QUICK_THINK_LLM=gpt-5.4-mini
TRADINGAGENTS_OPENAI_REASONING_EFFORT=high   # minimal | low | medium | high
TRADINGAGENTS_OUTPUT_LANGUAGE=English
```

Anthropic and Google use `TRADINGAGENTS_ANTHROPIC_EFFORT` and `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`. The full list of env-overridable keys lives in `_ENV_OVERRIDES` in [`tradingagents/default_config.py`](tradingagents/default_config.py).

#### Running batch mode in Docker

The Docker image's entrypoint is the `tradingagents` CLI, so `batch` works directly via the `cli` profile. The named volume `tradingagents_data` persists the decision log + cache + checkpoint DBs; when Postgres is up (the default), batches also persist into the `batches` + `runs` tables and survive container restarts.

```bash
docker compose --profile cli run --rm --no-TTY cli batch \
  NVDA AAPL MSFT GOOGL META AMZN \
  > "batch_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
```

On PowerShell:

```powershell
docker compose --profile cli run --rm --no-TTY cli batch `
  NVDA AAPL MSFT GOOGL META AMZN `
  *> "batch_$(Get-Date -Format yyyyMMdd_HHmmss).log"
```

`--no-TTY` is important when detaching or scheduling the run — the default compose service is set up with `tty: true` / `stdin_open: true` for the interactive CLI, which would otherwise block waiting for a terminal.

#### Where to look the next morning

- `<cwd>/reports/<TICKER>_<date>/complete_report.md` — per-section markdown bundle on the host (when `--save-reports` is on).
- Inside the Docker volume / `~/.tradingagents/`:
  - `logs/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json` — full pipeline state per ticker.
  - `memory/trading_memory.md` — one `pending` entry per ticker; resolves into a reflection on the next same-ticker run.
  - `cache/checkpoints/<TICKER>.db` — auto-deleted on success; a lingering DB marks an interrupted run you can resume by re-running the same ticker+date.

To extract the Docker volume's contents to the host:

```bash
docker run --rm -v tradingagents_data:/data -v "$(pwd)/out:/out" alpine \
  sh -c "cp -r /data/. /out/"
```

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, and Azure OpenAI for enterprise.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # openai, google, anthropic, xai, deepseek, qwen, qwen-cn, glm, glm-cn, minimax, minimax-cn, openrouter, ollama, azure
config["deep_think_llm"] = "gpt-5.4"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `tradingagents/default_config.py` for all configuration options.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Decision log

The decision log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. On the next run for the same ticker, TradingAgents fetches the realised return (raw and alpha vs SPY), generates a one-paragraph reflection, and injects the most recent same-ticker decisions plus recent cross-ticker lessons into the Portfolio Manager prompt, so each analysis carries forward what worked and what didn't.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. On a resume run you will see `Resuming from step N for <TICKER> on <date>` in the logs; on a new run you will see `Starting fresh`. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents analyze --checkpoint           # enable for this run
tradingagents analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

### Postgres + pgvector (optional)

For multi-user deployments, durable analysis history, and semantic
retrieval over past results, TradingAgents can persist to a PostgreSQL
database with the `pgvector` extension. The DB is **strictly opt-in** —
when `TRADINGAGENTS_DATABASE_URL` is unset the app behaves exactly as
before: in-memory state, markdown decision log, JSON cache files. Turning
the DB on doesn't require any code change; the same `tradingagents
serve` / `analyze` / `batch` commands route through Postgres
automatically when the env var resolves.

**What gets persisted:**

| Table | Contents |
| --- | --- |
| `runs` | One row per analysis: ticker, date, status, selections, decision, full final `AgentState` as JSONB. |
| `run_reports` | Per-section markdown — market, sentiment, news, fundamentals, competition, research_manager, trader, summary, etc. |
| `run_token_usage` | LLM call counts and token totals per run. |
| `decisions` | Replaces the markdown decision log. Pending → resolved transitions with realized return + reflection. |
| `run_embeddings` | pgvector cosine-indexed embeddings of each run's `summary`, `final_trade_decision`, `investment_plan` — feeds RAG injection into the Portfolio Manager prompt and the `search_past_analyses` tool. |
| `batches` | Batch submissions with their per-ticker progress (JSONB array under a row lock for safe concurrent updates). |
| `webui_settings` | The web UI's "last submitted selections" — replaces `webui_last_settings.json` when DB is on. |

**Setup (Docker — default):**

```bash
cp .env.example .env       # add at least one provider API key
docker compose up -d       # postgres + auto-migrate + web UI on :8000
```

That's it. The `migrate` service runs `python -m tradingagents.persistence upgrade` once and exits; `web` depends on it via `service_completed_successfully`, so the schema is always at head before traffic hits the server.

**Setup (host install):**

```bash
# 1. Bring up just Postgres via compose.
docker compose up -d postgres

# 2. Apply migrations from the host.
export TRADINGAGENTS_DATABASE_URL='postgresql+psycopg://tradingagents:tradingagents@localhost:5433/tradingagents'
python -m tradingagents.persistence upgrade

# 3. Run the app as usual — it'll automatically use the DB.
tradingagents serve
```

Migrations live at `tradingagents/persistence/migrations/`; current `head` is revision `0002`. Note the host-side port in the compose default is `5433` to avoid clashing with a host-side Postgres on `5432`; override with `POSTGRES_PORT` in `.env`.

**Embedding provider (Phase 4):**

The vector store needs an embedding provider. Default is OpenAI's
`text-embedding-3-small` (1536 dim) but can be switched via env vars:

```bash
#TRADINGAGENTS_EMBEDDING_PROVIDER=openai       # openai | google
#TRADINGAGENTS_EMBEDDING_MODEL=text-embedding-3-small
#TRADINGAGENTS_EMBEDDING_DIM=1536              # MUST match the model's output dim
```

When unset, the embedding provider follows the main `llm_provider` if
it's `openai` or `google`; otherwise it falls back to OpenAI (so
Anthropic/xAI/etc. users need `OPENAI_API_KEY` set as a baseline for
embeddings even though the main pipeline uses a different provider).

**Switching embedding providers** — pgvector columns have a fixed
width, so switching from a 1536-dim model to a 768-dim one (or vice
versa) requires re-embedding every existing run:

```bash
# 1. Update env vars; re-create the column if dim changed.
# 2. Re-embed all done runs through the new provider:
python scripts/reembed.py
```

`scripts/reembed.py` is resilient to per-row API errors — a flaky cell
is logged and skipped, the rest continue.

**Backfilling existing markdown decision logs:**

If you've been running without Postgres, an existing
`~/.tradingagents/memory/trading_memory.md` can be imported in one
shot:

```bash
python scripts/backfill_decisions.py
# Or with an explicit path:
python scripts/backfill_decisions.py /path/to/trading_memory.md
```

The script is idempotent — `UNIQUE(ticker, trade_date)` guards against
duplicate inserts, so re-running reports "N skipped, 0 inserted".

After the backfill, the markdown log is **sunset**: future writes go
to the `decisions` table only; the file stays on disk as a frozen
historical artifact. Same goes for `webui_last_settings.json` and
`webui_cache_*.json` — once the DB is on, the JSON files are neither
read nor written for new state.

**Multi-user concurrency:**

The web UI supports concurrent users — each analysis runs in its own
daemon thread with a `ContextVar`-scoped config so parallel runs don't
race each other's provider / vendor / output-language settings. The
DB writes are non-fatal: a transient Postgres outage degrades the
system to "no persistence happened this run" rather than failing the
analysis.

**Server-restart recovery:** any batch that was in flight when the
process died is marked `error` on the next process startup with a
payload note explaining the restart. Users see the failed batch in
Recent batches and can resubmit. (Real recovery via replaying the
deque is a future enhancement.)

## Contributing

We welcome contributions from the community! Whether it's fixing a bug, improving documentation, or suggesting a new feature, your input helps make this project better. If you are interested in this line of research, please consider joining our open-source financial AI research community [Tauric Research](https://tauric.ai/).

Past contributions, including code, design feedback, and bug reports, are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

Please reference our work if you find *TradingAgents* provides you with some help :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
