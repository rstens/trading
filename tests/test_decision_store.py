"""Tests for the decision-log Postgres replacement.

Covers:
  * The DAO functions directly (`store_pending_decision`,
    `resolve_decisions`, `get_pending_decisions`, `get_past_context_db`)
    via the live pg18 fixture.
  * The `TradingMemoryLog` delegation contract — DB on routes to DAO,
    DB off keeps writing/reading markdown.
  * The backfill script's parse → insert → idempotent re-run flow.
"""

from __future__ import annotations

import datetime
import importlib
from pathlib import Path

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


# ---------- DAO tests --------------------------------------------------


def test_store_pending_decision_inserts_and_is_idempotent(db_engine):
    from tradingagents.persistence import Decision, session_scope
    from tradingagents.persistence.decisions import store_pending_decision

    today = datetime.date.today().isoformat()
    assert store_pending_decision("NVDA", today, "FINAL TRANSACTION PROPOSAL: **BUY**.") is True
    # Second call same (ticker, trade_date) → idempotent, no second row.
    assert store_pending_decision("NVDA", today, "different text") is True

    with session_scope() as s:
        rows = s.query(Decision).filter_by(ticker="NVDA").all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].payload["rating"] == "Buy"
        # First write wins — second call's "different text" is ignored.
        assert "BUY" in rows[0].payload["decision_text"]


def test_store_pending_decision_returns_false_without_db(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.persistence.decisions import store_pending_decision

    assert store_pending_decision("X", "2026-05-17", "HOLD") is False


def test_get_pending_decisions_returns_only_pending(db_engine):
    from tradingagents.persistence.decisions import (
        get_pending_decisions, resolve_decisions, store_pending_decision,
    )

    today = datetime.date.today().isoformat()
    store_pending_decision("AAA", today, "BUY")
    store_pending_decision("BBB", today, "SELL")

    # Resolve AAA only.
    n = resolve_decisions([{
        "ticker": "AAA", "trade_date": today,
        "raw_return": 0.012, "alpha_return": 0.003,
        "holding_days": 5, "reflection": "Worked out.",
    }])
    assert n == 1

    pending = get_pending_decisions()
    tickers = {p["ticker"] for p in pending}
    assert tickers == {"BBB"}


def test_resolve_decisions_persists_outcome_fields(db_engine):
    from tradingagents.persistence import Decision, session_scope
    from tradingagents.persistence.decisions import (
        resolve_decisions, store_pending_decision,
    )

    today = datetime.date.today().isoformat()
    store_pending_decision("NVDA", today, "BUY")

    n = resolve_decisions([{
        "ticker": "NVDA", "trade_date": today,
        "raw_return": 0.025, "alpha_return": 0.008,
        "holding_days": 5, "reflection": "Played out as expected.",
        "benchmark": "SPY",
    }])
    assert n == 1

    with session_scope() as s:
        row = s.query(Decision).filter_by(ticker="NVDA").one()
        assert row.status == "resolved"
        assert row.resolved_at is not None
        assert row.payload["raw_return"] == 0.025
        assert row.payload["alpha_return"] == 0.008
        assert row.payload["holding_days"] == 5
        assert row.payload["benchmark"] == "SPY"
        assert "Played out" in row.payload["reflection"]


def test_resolve_decisions_skips_unknown_pairs(db_engine):
    """Calling resolve_decisions for a ticker/date pair with no pending
    row is a no-op, not an error."""
    from tradingagents.persistence.decisions import resolve_decisions

    today = datetime.date.today().isoformat()
    assert resolve_decisions([{
        "ticker": "GHOST", "trade_date": today,
        "raw_return": 0.0, "alpha_return": 0.0,
        "holding_days": 1, "reflection": "—",
    }]) == 0


def test_get_past_context_db_formats_same_and_cross(db_engine):
    from tradingagents.persistence.decisions import (
        get_past_context_db, resolve_decisions, store_pending_decision,
    )

    base = datetime.date.today() - datetime.timedelta(days=20)
    # Three same-ticker (NVDA) resolved entries, two cross-ticker.
    for i, ticker in enumerate(["NVDA", "NVDA", "NVDA", "AAPL", "MSFT"]):
        d = (base + datetime.timedelta(days=i)).isoformat()
        store_pending_decision(ticker, d, f"FINAL: **BUY** {ticker}")
        resolve_decisions([{
            "ticker": ticker, "trade_date": d,
            "raw_return": 0.01 * (i + 1),
            "alpha_return": 0.002 * (i + 1),
            "holding_days": 5,
            "reflection": f"{ticker} reflection #{i}.",
        }])

    ctx = get_past_context_db("NVDA", n_same=5, n_cross=3)
    # Same-ticker section present, decisions formatted with the
    # legacy markdown shape (DECISION:, REFLECTION:).
    assert "Past analyses of NVDA" in ctx
    assert "DECISION:" in ctx
    assert "REFLECTION:" in ctx
    # Cross-ticker section present.
    assert "Recent cross-ticker lessons" in ctx
    assert "AAPL" in ctx
    assert "MSFT" in ctx


def test_get_past_context_db_empty_returns_empty_string(db_engine):
    from tradingagents.persistence.decisions import get_past_context_db
    assert get_past_context_db("NEW") == ""


# ---------- TradingMemoryLog delegation -------------------------------


def test_memory_log_delegates_to_db_when_on(db_engine, tmp_path):
    """With DB on, store_decision writes to the DB, NOT to the markdown file."""
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.persistence import Decision, session_scope

    md = tmp_path / "trading_memory.md"
    log = TradingMemoryLog({"memory_log_path": str(md)})
    today = datetime.date.today().isoformat()
    log.store_decision("NVDA", today, "FINAL TRANSACTION PROPOSAL: **HOLD**.")

    # Markdown file MUST NOT have been written (sunset behavior).
    assert not md.exists(), "Markdown file written despite DB being configured"

    # DB row present.
    with session_scope() as s:
        row = s.query(Decision).filter_by(ticker="NVDA").one()
        assert row.status == "pending"


def test_memory_log_falls_back_to_markdown_when_no_db(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.agents.utils.memory import TradingMemoryLog

    md = tmp_path / "trading_memory.md"
    log = TradingMemoryLog({"memory_log_path": str(md)})
    today = datetime.date.today().isoformat()
    log.store_decision("NVDA", today, "FINAL TRANSACTION PROPOSAL: **BUY**.")

    # Markdown was the only store when DB is off.
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert f"[{today} | NVDA |" in content
    assert "pending" in content


def test_memory_log_get_past_context_uses_db_when_on(db_engine, tmp_path):
    """When DB has resolved entries, get_past_context routes through DB
    even if a stale markdown file exists on disk."""
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.persistence.decisions import (
        resolve_decisions, store_pending_decision,
    )

    today = datetime.date.today().isoformat()
    store_pending_decision("NVDA", today, "BUY")
    resolve_decisions([{
        "ticker": "NVDA", "trade_date": today,
        "raw_return": 0.01, "alpha_return": 0.003,
        "holding_days": 5, "reflection": "OK.",
    }])

    # Seed an unrelated markdown file with bogus content that must NOT
    # show up in the result.
    md = tmp_path / "trading_memory.md"
    md.write_text("[2099-01-01 | BOGUS | Buy | pending]\n\nDECISION:\nfake\n\n<!-- ENTRY_END -->\n\n",
                  encoding="utf-8")
    log = TradingMemoryLog({"memory_log_path": str(md)})

    ctx = log.get_past_context("NVDA")
    assert "NVDA" in ctx
    assert "BOGUS" not in ctx  # markdown bypassed when DB is on


# ---------- Backfill script -------------------------------------------


def _seed_markdown(path: Path) -> None:
    """Write a minimal valid markdown decision log."""
    sep = "\n\n<!-- ENTRY_END -->\n\n"
    body = (
        "[2026-04-01 | NVDA | Buy | +1.2% | +0.3% | 5d]\n\n"
        "DECISION:\nBUY NVDA — strong fundamentals.\n\n"
        "REFLECTION:\nWorked as expected." + sep +
        "[2026-04-15 | AAPL | Hold | pending]\n\n"
        "DECISION:\nHOLD AAPL." + sep
    )
    path.write_text(body, encoding="utf-8")


def test_backfill_imports_resolved_and_pending(db_engine, tmp_path):
    from tradingagents.persistence import Decision, session_scope
    from scripts.backfill_decisions import backfill

    md = tmp_path / "trading_memory.md"
    _seed_markdown(md)

    inserted, skipped, malformed = backfill(md)
    assert inserted == 2
    assert skipped == 0
    assert malformed == 0

    with session_scope() as s:
        rows = {r.ticker: r for r in s.query(Decision).all()}
        assert rows.keys() == {"NVDA", "AAPL"}

        nvda = rows["NVDA"]
        assert nvda.status == "resolved"
        assert nvda.payload["raw_return"] == pytest.approx(0.012)
        assert nvda.payload["alpha_return"] == pytest.approx(0.003)
        assert nvda.payload["holding_days"] == 5
        assert "Worked as expected" in nvda.payload["reflection"]

        aapl = rows["AAPL"]
        assert aapl.status == "pending"
        assert "decision_text" in aapl.payload


def test_backfill_is_idempotent(db_engine, tmp_path):
    from scripts.backfill_decisions import backfill

    md = tmp_path / "trading_memory.md"
    _seed_markdown(md)

    inserted1, skipped1, _ = backfill(md)
    inserted2, skipped2, _ = backfill(md)

    assert inserted1 == 2
    assert skipped1 == 0
    assert inserted2 == 0  # all duplicates on second pass
    assert skipped2 == 2
