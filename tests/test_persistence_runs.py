"""Tests for the DAO helpers in `tradingagents.persistence.runs`.

DB-gated like every other persistence test — skipped locally without
``TRADINGAGENTS_TEST_DATABASE_URL``, run in CI against a pgvector
container.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


def _today() -> str:
    return datetime.date.today().isoformat()


def test_insert_run_returns_uuid(db_engine):
    from tradingagents.persistence.runs import insert_run

    rid = insert_run(
        ticker="NVDA",
        analysis_date=_today(),
        selections={"llm_provider": "openai", "ticker": "NVDA"},
    )
    assert rid is not None
    assert isinstance(rid, uuid.UUID)


def test_insert_run_returns_none_without_db(monkeypatch):
    """No DB configured → insert_run returns None instead of raising."""
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.persistence.runs import insert_run

    rid = insert_run(
        ticker="NVDA",
        analysis_date=_today(),
        selections={"llm_provider": "openai"},
    )
    assert rid is None


def test_persist_run_completion_round_trip(db_engine):
    """End-to-end: insert → finalize → DB has terminal row + reports + usage."""
    from tradingagents.persistence import Run, session_scope
    from tradingagents.persistence.runs import insert_run, persist_run_completion

    rid = insert_run(
        ticker="NVDA",
        analysis_date=_today(),
        selections={"llm_provider": "openai", "ticker": "NVDA"},
    )
    assert rid is not None

    ok = persist_run_completion(
        rid,
        status="done",
        decision="FINAL TRANSACTION PROPOSAL: **BUY**\n\nStrong fundamentals.",
        error=None,
        traceback=None,
        raw_state={"company_of_interest": "NVDA", "final_trade_decision": "BUY"},
        partial_state={
            "market_report": "Market is bullish.",
            "summary": "Buy in tranches.",
            "empty_one": "",  # skipped
        },
        usage={"llm_calls": 12, "tokens_in": 5000, "tokens_out": 800},
        company_name="NVIDIA Corporation",
    )
    assert ok is True

    with session_scope() as s:
        run = s.get(Run, rid)
        assert run is not None
        assert run.status == "done"
        # parse_rating returns capitalized form ("Buy" not "BUY").
        assert run.rating == "Buy"
        assert run.company_name == "NVIDIA Corporation"
        assert run.finished_at is not None
        assert run.raw_state["final_trade_decision"] == "BUY"

        # Only non-empty sections persisted.
        report_sections = {r.section: r.content for r in run.reports}
        assert "market_report" in report_sections
        assert "summary" in report_sections
        assert "empty_one" not in report_sections

        # Token usage row created.
        assert run.token_usage is not None
        assert run.token_usage.usage["llm_calls"] == 12


def test_find_cache_hit_db_returns_recent_done(db_engine):
    from tradingagents.persistence.runs import (
        find_cache_hit_db, insert_run, persist_run_completion,
    )

    today = _today()
    rid = insert_run(
        ticker="NVDA", analysis_date=today,
        selections={"llm_provider": "openai", "ticker": "NVDA"},
    )
    persist_run_completion(
        rid, status="done", decision="BUY", error=None, traceback=None,
        raw_state={"x": 1}, partial_state={"summary": "ok"},
        usage={"llm_calls": 1}, company_name="NVIDIA Corporation",
    )

    hit = find_cache_hit_db("NVDA", today)
    assert hit is not None
    assert hit["run_id"] == rid
    assert hit["partial_state"]["summary"] == "ok"
    assert hit["company_name"] == "NVIDIA Corporation"
    assert hit["finished_at"] is not None


def test_find_cache_hit_db_misses_on_old_run(db_engine):
    """A run older than the TTL is not a cache hit."""
    from sqlalchemy import update

    from tradingagents.persistence import Run, session_scope
    from tradingagents.persistence.runs import (
        find_cache_hit_db, insert_run, persist_run_completion,
    )

    today = _today()
    rid = insert_run(
        ticker="OLDX", analysis_date=today,
        selections={"llm_provider": "openai", "ticker": "OLDX"},
    )
    persist_run_completion(
        rid, status="done", decision="BUY", error=None, traceback=None,
        raw_state={}, partial_state={}, usage={},
    )

    # Backdate finished_at past the TTL.
    very_old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)
    with session_scope() as s:
        s.execute(update(Run).where(Run.id == rid).values(finished_at=very_old))

    assert find_cache_hit_db("OLDX", today) is None


def test_find_cache_hit_db_misses_on_non_done_status(db_engine):
    """Cancelled / error runs are not served as cache hits."""
    from tradingagents.persistence.runs import (
        find_cache_hit_db, insert_run, persist_run_completion,
    )

    today = _today()
    rid = insert_run(
        ticker="CNCL", analysis_date=today,
        selections={"llm_provider": "openai", "ticker": "CNCL"},
    )
    persist_run_completion(
        rid, status="cancelled", decision=None, error="user stop",
        traceback=None, raw_state=None, partial_state={"market_report": "partial"},
        usage={},
    )
    assert find_cache_hit_db("CNCL", today) is None


def test_list_recent_terminal_runs_returns_in_descending_started_order(db_engine):
    from tradingagents.persistence.runs import (
        insert_run, list_recent_terminal_runs, persist_run_completion,
    )

    today = _today()
    # Insert three runs, each in a different state.
    r1 = insert_run("AAA", today, {"ticker": "AAA"})
    persist_run_completion(r1, status="done", decision="BUY",
                            error=None, traceback=None, raw_state=None,
                            partial_state={}, usage={})
    r2 = insert_run("BBB", today, {"ticker": "BBB"})
    persist_run_completion(r2, status="error", decision=None,
                            error="boom", traceback="trace",
                            raw_state=None, partial_state={}, usage={})
    r3 = insert_run("CCC", today, {"ticker": "CCC"})
    persist_run_completion(r3, status="cancelled", decision=None,
                            error="stopped", traceback=None,
                            raw_state=None, partial_state={}, usage={})

    rows = list_recent_terminal_runs(limit=10)
    # Three terminals total.
    assert len(rows) == 3
    # Latest started first — order should be CCC > BBB > AAA.
    tickers_in_order = [r.ticker for r in rows]
    assert tickers_in_order == ["CCC", "BBB", "AAA"]
    # status_label mapping: cancelled → "Stop".
    cancelled = [r for r in rows if r.ticker == "CCC"][0]
    assert cancelled.status_label == "Stop"


def test_list_recent_terminal_runs_excludes_running(db_engine):
    """A run still in `running` state isn't returned by the recent-terminal query."""
    from tradingagents.persistence.runs import insert_run, list_recent_terminal_runs

    today = _today()
    insert_run("RUNNING", today, {"ticker": "RUNNING"})  # stays in running

    rows = list_recent_terminal_runs(limit=10)
    assert all(r.ticker != "RUNNING" for r in rows)
