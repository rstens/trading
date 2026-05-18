"""Phase 7 — webui/preferences.py round-trips through Postgres when
``TRADINGAGENTS_DATABASE_URL`` is configured, falls back to the JSON
file when it isn't.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


# ---------- DAO tests (no preferences wrapper) -----------------------


def test_save_settings_db_upserts(db_engine):
    from tradingagents.persistence import WebUISettings, session_scope
    from tradingagents.persistence.settings import (
        load_settings_db, save_settings_db,
    )

    # First write inserts the row.
    assert save_settings_db({"llm_provider": "openai", "ticker": "NVDA"}) is True
    loaded = load_settings_db()
    assert loaded["llm_provider"] == "openai"
    assert loaded["ticker"] == "NVDA"

    # Second write updates the same row (singleton CHECK enforces id=1).
    assert save_settings_db({"llm_provider": "anthropic", "ticker": "AAPL"}) is True
    with session_scope() as s:
        rows = s.query(WebUISettings).all()
        assert len(rows) == 1
        assert rows[0].selections["llm_provider"] == "anthropic"


def test_save_settings_db_returns_false_without_db(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.persistence.settings import (
        load_settings_db, save_settings_db,
    )
    assert save_settings_db({"x": 1}) is False
    assert load_settings_db() is None


def test_load_settings_db_returns_none_when_row_missing(db_engine):
    from tradingagents.persistence.settings import load_settings_db
    assert load_settings_db() is None


# ---------- webui/preferences.py end-to-end --------------------------


def test_preferences_round_trip_via_db(db_engine, tmp_path, monkeypatch):
    """When DB is configured, save/load round-trips through Postgres
    and the JSON file is never created."""
    # Point the legacy file path at a temp directory so we can detect
    # any stray writes.
    import webui.preferences as prefs
    file_path = tmp_path / "webui_last_settings.json"
    monkeypatch.setattr(prefs, "_FILE", file_path)

    sample = {
        "ticker": "NVDA",
        "llm_provider": "openai",
        "quick_thinker": "gpt-5.4-mini",
        "deep_thinker": "gpt-5.4",
        "analysts": ["market", "news"],
        "research_depth": 2,
        "output_language": "English",
        "openai_reasoning_effort": "high",
        # Non-persisted keys should be filtered out by PERSISTED_FIELDS.
        "force_refresh": True,
        "analysis_date": "2026-05-17",
    }
    prefs.save_last_settings(sample)

    # No file write when DB is on.
    assert not file_path.exists(), "file written despite DB being configured"

    loaded = prefs.load_last_settings()
    assert loaded["ticker"] == "NVDA"
    assert loaded["llm_provider"] == "openai"
    assert loaded["analysts"] == ["market", "news"]
    assert loaded["research_depth"] == 2
    assert loaded["openai_reasoning_effort"] == "high"
    # Non-persisted keys filtered out.
    assert "force_refresh" not in loaded
    assert "analysis_date" not in loaded


def test_preferences_db_load_ignores_stale_json_file(db_engine, tmp_path, monkeypatch):
    """An existing JSON file from a prior no-DB run is left alone but
    NOT read when DB is on."""
    import webui.preferences as prefs

    file_path = tmp_path / "webui_last_settings.json"
    file_path.write_text(json.dumps({"ticker": "OLD_FROM_FILE"}), encoding="utf-8")
    monkeypatch.setattr(prefs, "_FILE", file_path)

    # Nothing in DB yet → load returns empty even though the file
    # has stale content.
    loaded = prefs.load_last_settings()
    assert loaded == {}

    # File still exists (we don't delete it — frozen artifact).
    assert file_path.exists()
    assert json.loads(file_path.read_text())["ticker"] == "OLD_FROM_FILE"


def test_preferences_round_trip_via_file_when_db_off(tmp_path, monkeypatch):
    """No DB → save/load round-trips through the JSON file."""
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    import webui.preferences as prefs
    file_path = tmp_path / "webui_last_settings.json"
    monkeypatch.setattr(prefs, "_FILE", file_path)

    prefs.save_last_settings({"ticker": "AAPL", "llm_provider": "anthropic"})
    assert file_path.exists()
    loaded = prefs.load_last_settings()
    assert loaded["ticker"] == "AAPL"
    assert loaded["llm_provider"] == "anthropic"


def test_preferences_save_filters_to_persisted_fields_only(db_engine, tmp_path, monkeypatch):
    """Junk keys outside PERSISTED_FIELDS get dropped on save."""
    import webui.preferences as prefs
    monkeypatch.setattr(prefs, "_FILE", tmp_path / "webui_last_settings.json")

    prefs.save_last_settings({
        "ticker": "MSFT",
        "secret_api_key": "should-never-be-stored",   # not in PERSISTED_FIELDS
        "raw_internal_state": {"big": "blob"},        # not in PERSISTED_FIELDS
        "llm_provider": "openai",
    })

    from tradingagents.persistence import WebUISettings, session_scope
    with session_scope() as s:
        row = s.get(WebUISettings, 1)
        assert "secret_api_key" not in row.selections
        assert "raw_internal_state" not in row.selections
        assert row.selections["ticker"] == "MSFT"
        assert row.selections["llm_provider"] == "openai"


def test_preferences_db_save_handles_empty_dict(db_engine, tmp_path, monkeypatch):
    """A save with nothing matching PERSISTED_FIELDS writes an empty row."""
    import webui.preferences as prefs
    monkeypatch.setattr(prefs, "_FILE", tmp_path / "webui_last_settings.json")

    prefs.save_last_settings({"random_unrelated_key": "x"})
    loaded = prefs.load_last_settings()
    assert loaded == {}
