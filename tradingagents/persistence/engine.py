"""Lazy SQLAlchemy engine factory.

Returns ``None`` when no ``TRADINGAGENTS_DATABASE_URL`` is configured —
the rest of the persistence layer treats that as "skip writes". This
keeps `tradingagents serve` ergonomic in dev without a running Postgres.

The engine + session factory are process-singletons. Construction is
thread-safe: ``threading.Lock`` guards the first-time init.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


log = logging.getLogger("tradingagents.persistence")


def _current_db_url() -> Optional[str]:
    """Read the active database URL from DEFAULT_CONFIG fresh on every call.

    We deliberately do NOT do `from tradingagents.default_config import
    DEFAULT_CONFIG` at module load — that captures a reference to a
    specific dict instance, which `importlib.reload(default_config)`
    (used in some tests) leaves stale. By dereferencing through the
    module object each time, we always see the current dict.
    """
    import tradingagents.default_config as dc  # noqa: WPS433
    return dc.DEFAULT_CONFIG.get("database_url")


_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker[Session]] = None
_init_lock = threading.Lock()
_warned_about_missing_db = False


def get_engine() -> Optional[Engine]:
    """Return the singleton engine, or None when DB is not configured.

    The "no DB configured" warning is logged exactly once per process
    so users see the fallback notice at startup without log spam.
    """
    global _engine, _session_factory, _warned_about_missing_db

    if _engine is not None:
        return _engine

    with _init_lock:
        # Re-check after acquiring the lock — another thread may have
        # initialized while we were blocked.
        if _engine is not None:
            return _engine

        url = _current_db_url()
        if not url:
            if not _warned_about_missing_db:
                log.warning(
                    "TRADINGAGENTS_DATABASE_URL not set — running with in-memory "
                    "state + markdown decision-log fallback. No analyses, "
                    "decisions, or embeddings will be persisted.",
                )
                _warned_about_missing_db = True
            return None

        # `pool_pre_ping=True` so reconnects survive Postgres restarts
        # without bubbling up "MySQL server has gone away"-style errors.
        # `future=True` is the SA 2.0 default but explicit here.
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
        log.info("Persistence: connected via %s", _safe_url(url))
        return _engine


def get_session_factory() -> Optional[sessionmaker[Session]]:
    """Session factory tied to the singleton engine, or None when no DB."""
    if get_engine() is None:
        return None
    return _session_factory


def _safe_url(url: str) -> str:
    """Mask the password component of a DB URL for logging."""
    try:
        from sqlalchemy.engine.url import make_url

        u = make_url(url)
        if u.password:
            u = u.set(password="***")
        return str(u)
    except Exception:  # noqa: BLE001
        # Best-effort: don't fail logging because of a malformed URL.
        return "<db-url>"


def _reset_for_tests() -> None:
    """Reset the module-global engine cache. Test-only utility."""
    global _engine, _session_factory, _warned_about_missing_db
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _warned_about_missing_db = False
