"""Cache of completed web-UI analyses for fast re-retrieval.

After each successful pipeline + summary completes, the runner dumps the
full JobState (final_state, partial_state including summary, decision,
company name, selections) to a per-ticker JSON file. A subsequent
submission for the same `(ticker, analysis_date)` within the cache TTL
short-circuits the LangGraph pipeline and renders the cached results
immediately — sparing the user 5–10 minutes of wall-clock time and the
API tokens of every agent's LLM call.

Layout under ``results_dir`` (defaults to ``~/.tradingagents/logs/``):

    <results_dir>/
      <safe_ticker>/
        webui_cache_<analysis_date>.json   ← this module's artifact
        TradingAgentsStrategy_logs/        ← canonical graph state (untouched)
          full_states_log_<analysis_date>.json

The cache is keyed by ``(ticker, analysis_date)`` and TTL-gated by file
mtime — so a cache file written more than ``CACHE_TTL_SECONDS`` ago is
ignored even though it sits on disk (the file isn't deleted; the next
fresh run just overwrites it).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from tradingagents.dataflows.utils import safe_ticker_component


CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _cache_dir(results_dir: str, ticker: str) -> Path:
    return Path(results_dir) / safe_ticker_component(ticker)


def _cache_file(results_dir: str, ticker: str, analysis_date: str) -> Path:
    return _cache_dir(results_dir, ticker) / f"webui_cache_{analysis_date}.json"


def find_cache_hit(
    results_dir: str,
    ticker: str,
    analysis_date: str,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> Optional[Path]:
    """Return the cache file for `(ticker, analysis_date)` within TTL, else None."""
    path = _cache_file(results_dir, ticker, analysis_date)
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl_seconds:
        return None
    return path


def load_cache(path: Path) -> Dict[str, Any]:
    """Read a cache file. Caller handles JSONDecodeError / OSError."""
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(
    results_dir: str,
    ticker: str,
    analysis_date: str,
    payload: Dict[str, Any],
) -> Path:
    """Atomically write the cache file. Returns the final path."""
    directory = _cache_dir(results_dir, ticker)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"webui_cache_{analysis_date}.json"

    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".webui_cache_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str, ensure_ascii=False)
        os.replace(tmp, final)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    return final


def json_safe(value: Any) -> Any:
    """Recursive coercion of values into JSON-serializable form.

    Same shape as the previous `_json_safe` helper in routes.py — moved
    here so save_cache and the export endpoint can both use it.
    Handles LangChain Message objects (via `model_dump` / `dict`),
    Pydantic models, dataclasses, datetimes, and falls back to `str()`
    for anything else.
    """
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        try:
            return json_safe(value.dict())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
