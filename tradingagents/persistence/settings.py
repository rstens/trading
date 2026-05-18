"""DAO for the `webui_settings` singleton row.

When the DB is configured, the web UI's "last submitted selections"
storage moves from `~/.tradingagents/webui_last_settings.json` to a
single row in Postgres (id=1, enforced by a CHECK constraint declared
in `models.py`). Multi-user deployments share that one row — there's
still no per-user scoping (auth is deferred to a future phase).

Both functions are no-ops returning ``None`` / ``False`` when the DB
isn't configured. `webui/preferences.py` falls back to the JSON file in
that case.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

from tradingagents.persistence.models import WebUISettings
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.settings")


def load_settings_db() -> Optional[Dict[str, Any]]:
    """Return the singleton row's selections, or None when:

    * DB is unconfigured (session_scope yields None)
    * the row doesn't exist yet (nothing's been saved)
    * the read raises (logged, swallowed)

    Caller (webui/preferences.py) treats None as "no DB-persisted
    selections; fall through to the JSON file or return defaults".
    """
    try:
        with session_scope() as s:
            if s is None:
                return None
            row = s.get(WebUISettings, 1)
            if row is None:
                return None
            return dict(row.selections or {})
    except Exception as e:  # noqa: BLE001
        log.warning("load_settings_db failed: %s", e)
        return None


def save_settings_db(selections: Dict[str, Any]) -> bool:
    """Upsert the singleton row. True on success, False on no-DB / failure."""
    try:
        with session_scope() as s:
            if s is None:
                return False
            row = s.get(WebUISettings, 1)
            if row is None:
                s.add(WebUISettings(id=1, selections=dict(selections or {})))
            else:
                row.selections = dict(selections or {})
                # ORM onupdate handles updated_at; setting it explicitly
                # would no-op in SQLAlchemy 2.x but isn't harmful.
                row.updated_at = datetime.datetime.now(datetime.timezone.utc)
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("save_settings_db failed: %s", e)
        return False
