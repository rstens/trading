"""Persist the last-used selection form values across server restarts.

Two storage modes:

* **DB on** (``TRADINGAGENTS_DATABASE_URL`` set) — reads / writes the
  ``webui_settings`` singleton row in Postgres. The JSON file at
  ``~/.tradingagents/webui_last_settings.json`` is neither read nor
  written; an existing file from a prior no-DB run is left in place
  as a frozen artifact.
* **DB off** — legacy JSON-file behavior: atomic temp + rename to
  ``webui_last_settings.json`` so a crash mid-write doesn't leave a
  half-written file.

Only form fields are persisted — never API keys, never job state. The
analysis date is intentionally excluded so each new run starts pointing
at "today" rather than the day the user last opened the page.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from tradingagents.default_config import DEFAULT_CONFIG


log = logging.getLogger("tradingagents.webui.preferences")


# `data_cache_dir` defaults to ~/.tradingagents/cache; its parent is the
# project root under the user's home that the rest of the app already uses.
_FILE = Path(DEFAULT_CONFIG["data_cache_dir"]).parent / "webui_last_settings.json"


PERSISTED_FIELDS = (
    "ticker",
    "analysts",
    "llm_provider",
    "quick_thinker",
    "deep_thinker",
    "research_depth",
    "output_language",
    "openai_reasoning_effort",
    "anthropic_effort",
    "google_thinking_level",
)


def _filter_persisted(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (raw or {}).items() if k in PERSISTED_FIELDS}


def load_last_settings() -> Dict[str, Any]:
    """Return saved selections, or an empty dict if none exist."""
    # DB path: when configured, the singleton row is canonical and the
    # JSON file (if present from prior no-DB use) is ignored.
    try:
        from tradingagents.persistence.decisions import db_is_configured
        if db_is_configured():
            from tradingagents.persistence.settings import load_settings_db
            data = load_settings_db()
            return _filter_persisted(data or {})
    except Exception as e:  # noqa: BLE001
        # Persistence-module import failure (e.g. SQLAlchemy not
        # installed in some stripped-down deployment) → fall through
        # to the file path.
        log.warning("DB settings load unavailable, falling back to file: %s", e)

    # File path: the legacy behavior preserved verbatim.
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return _filter_persisted(data)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_last_settings(selections: Dict[str, Any]) -> None:
    """Persist `selections` (filtered to PERSISTED_FIELDS) to whichever
    backend is active for this process."""
    out = _filter_persisted(selections)

    # DB path: writes to the singleton row. Markdown / JSON fallback
    # files are NOT updated when DB is on — single source of truth.
    try:
        from tradingagents.persistence.decisions import db_is_configured
        if db_is_configured():
            from tradingagents.persistence.settings import save_settings_db
            save_settings_db(out)
            return
    except Exception as e:  # noqa: BLE001
        log.warning("DB settings save unavailable, falling back to file: %s", e)

    # File path: atomic write via tempfile + os.replace.
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_FILE.parent, prefix=".webui_settings_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, _FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
