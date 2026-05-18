"""Tests for the persistence engine + session-scope.

The "no DB configured" path is exercised without any Postgres install.
The "DB connected" path is gated on TRADINGAGENTS_TEST_DATABASE_URL via
the shared db_engine fixture (skipped locally, run in CI).
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text

# Re-import fixtures so pytest discovers them.
from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


def test_engine_returns_none_when_url_unset(monkeypatch):
    """No `database_url` in DEFAULT_CONFIG → engine factory returns None.

    Important: do NOT importlib.reload(default_config) here — that would
    rebind DEFAULT_CONFIG to a new dict, while the engine module still
    references the old one (it captured it via `from … import
    DEFAULT_CONFIG` at module load). Direct monkeypatch.setitem on the
    existing dict is what every other test relies on.
    """
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    assert eng.get_engine() is None
    assert eng.get_session_factory() is None


def test_engine_warning_is_logged_once(monkeypatch, caplog):
    """The 'no DB' warning is logged on first call, not on subsequent ones."""
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    import tradingagents.default_config as dc
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(dc.DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    with caplog.at_level("WARNING", logger="tradingagents.persistence"):
        eng.get_engine()
        eng.get_engine()
        eng.get_engine()

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "TRADINGAGENTS_DATABASE_URL" in r.message
    ]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"


def test_session_scope_yields_none_when_no_db(monkeypatch):
    """session_scope() yields None (not an error) when DB is unset."""
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    import tradingagents.default_config as dc
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(dc.DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.persistence import session_scope

    with session_scope() as s:
        assert s is None


def test_engine_connects_when_url_set(db_engine):
    """With a real Postgres URL, engine yields a working connection."""
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_session_scope_commits_on_clean_exit(db_engine):
    """Session inserts persist past the context manager."""
    from tradingagents.persistence import WebUISettings, session_scope

    with session_scope() as s:
        assert s is not None
        s.add(WebUISettings(id=1, selections={"foo": "bar"}))

    with session_scope() as s:
        row = s.get(WebUISettings, 1)
        assert row is not None
        assert row.selections == {"foo": "bar"}


def test_session_scope_rolls_back_on_exception(db_engine):
    """An exception inside the with-block rolls back the transaction."""
    from tradingagents.persistence import WebUISettings, session_scope

    with pytest.raises(ValueError):
        with session_scope() as s:
            assert s is not None
            s.add(WebUISettings(id=1, selections={"foo": "bar"}))
            raise ValueError("aborted")

    with session_scope() as s:
        row = s.get(WebUISettings, 1)
        assert row is None
