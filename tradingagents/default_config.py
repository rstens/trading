import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":            "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":          "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":         "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":         "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":         "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":       "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":         "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":      "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":        "benchmark_ticker",
    # Provider-specific reasoning/thinking effort. Coerced as plain strings
    # (the reference defaults are None, so _coerce returns the value
    # verbatim). The receiving client validates the actual value.
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    # Persistence (Phase 1+). When `database_url` is unset, the app falls
    # back to in-memory state + markdown decision log — see
    # `tradingagents.persistence.engine`.
    "TRADINGAGENTS_DATABASE_URL":            "database_url",
    "TRADINGAGENTS_EMBEDDING_PROVIDER":      "embedding_provider",
    "TRADINGAGENTS_EMBEDDING_MODEL":         "embedding_model",
    "TRADINGAGENTS_EMBEDDING_DIM":           "embedding_dim",
}


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value."""
    if isinstance(reference, bool):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        config[key] = _coerce(raw, config.get(key))
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Articles fetched per search query. Queries are consulted top to
    # bottom, taking up to this many fresh articles each, until
    # global_news_article_limit is reached — so order the query lists by
    # priority. (Before this knob existed the first query alone filled the
    # whole limit and the rest of the list never ran.)
    "global_news_articles_per_query": 2,
    # Default macro queries used by get_global_news for most tickers.
    # Extend or replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Canada-focused queries, used instead of the default set when the
    # analyzed ticker is a Canadian listing (.TO / .V). Ordered by priority.
    "global_news_queries_canada": [
        "Bank of Canada interest rate decision overnight rate",
        "Canada GDP growth Statistics Canada economic outlook",
        "Canada CPI inflation consumer price index",
        "Statistics Canada Labour Force Survey unemployment rate",
        "Canadian dollar loonie USD exchange rate forecast",
        "Canada retail sales consumer spending data",
        "Canada trade balance exports imports surplus deficit",
        "Canada economy recession risk growth forecast",
        "RBC TD BMO Scotiabank CIBC bank earnings",
        "OSFI bank capital requirements mortgage stress test",
        "TSX S&P composite Canadian stock market",
        "Canadian banks loan loss provisions credit quality",
        "Alberta oil sands crude production WCS price",
        "Canada LNG natural gas export terminal",
        "Trans Mountain Enbridge pipeline capacity oil",
        "Canada mining gold copper critical minerals",
        "Canada potash uranium commodity prices",
        "Canada softwood lumber forestry exports duties",
        "US Canada tariffs trade dispute escalation",
        "Trump tariffs Canadian steel aluminum autos",
        "CUSMA USMCA review renegotiation trade agreement",
        "Canada retaliatory tariffs countermeasures US",
        "Canada defense spending NATO two percent target",
        "Canadian military procurement F-35 fighter jets",
        "Canada naval shipbuilding surface combatant program",
        "NORAD modernization Arctic security Canada",
        "Canadian defense industry contracts suppliers",
        "Canada federal budget Parliament fiscal policy",
        "Canada federal government policy Prime Minister",
        "Canada provincial politics Alberta Ontario Quebec",
        "Canada immigration levels targets policy",
        "Canada federal deficit government spending debt",
        "Canada housing market home prices CMHC",
        "Canadian mortgage rates housing affordability",
        "Toronto Vancouver real estate housing starts",
        "Canada housing supply construction starts permits",
        "Canada job market hiring layoffs employment",
        "Canada wage growth labour shortage vacancies",
        "Canadian companies mergers acquisitions deals",
        "Canada foreign investment partnership agreement",
        "Canada EU CETA Indo-Pacific trade diversification",
        "Canada US cross-border business investment",
        "Canadian corporate earnings TSX listed companies",
        "Canada auto industry manufacturing Ontario",
        "Canada technology sector AI startups funding",
        "Canada agriculture agri-food exports sector",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
        # Macro headlines come from newsdata.io (proper query/country/
        # category filtering — much better relevance than yfinance's fuzzy
        # Search). Requires NEWSDATA_API_KEY in .env; when the key is
        # missing the implementation falls back to yfinance automatically.
        "get_global_news": "newsdata",
    },
    # Request filters for the newsdata.io vendor (get_global_news, served
    # by the market-news endpoint /api/1/market — already finance-scoped,
    # so there is no category filter). `country` applies to the default
    # macro set; `country_canada` when the analyzed ticker is a Canadian
    # listing (.TO / .V).
    "newsdata_params": {
        "language": "en",
        "country": "us,gb",
        "country_canada": "ca,us",
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",    # NSE India (Nifty 50)
        ".BO":  "^BSESN",   # BSE India (Sensex)
        ".T":   "^N225",    # Tokyo (Nikkei 225)
        ".HK":  "^HSI",     # Hong Kong (Hang Seng)
        ".L":   "^FTSE",    # London (FTSE 100)
        ".TO":  "^GSPTSE",  # Toronto (TSX Composite)
        ".AX":  "^AXJO",    # Australia (ASX 200)
        "":     "SPY",      # default for US-listed tickers (no suffix)
    },
    # Persistence settings. `database_url` unset → degrade to in-memory +
    # markdown fallback (no Postgres writes). When set, the persistence
    # package owns runs / decisions / batches / vector embeddings.
    "database_url":      None,
    # Embedding-provider routing (Phase 4). Resolved at config load time
    # from the main `llm_provider` when None:
    #   openai / google → use same provider for embeddings
    #   anything else  → fall back to openai (decision #1)
    # The dimension is a deployment-level constant — pgvector columns are
    # declared at this width. Switching after the fact requires a re-embed
    # (scripts/reembed.py ships in Phase 4).
    "embedding_provider": None,
    "embedding_model":    None,
    "embedding_dim":      1536,
})
