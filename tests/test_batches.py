"""Tests for Phase 6 — batches persistence.

DAO-level tests for `insert_batch`, `update_batch_status`,
`update_batch_ticker` (including row-locking semantics under
concurrency), `list_batches_db`, `interrupt_in_flight_batches`. Plus a
BatchRegistry integration test verifying that creating a batch writes a
row and the dispatcher's mutations propagate.

All DB-gated. Mocks the per-ticker job (we're testing batches, not the
LangGraph run).
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid
from unittest.mock import patch

import pytest

from tests._persistence_fixtures import db_engine, db_url  # noqa: F401


# ---------- DAO tests ------------------------------------------------


def test_insert_batch_returns_uuid_and_writes_payload(db_engine):
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import insert_batch

    bid = insert_batch(
        base_selections={"llm_provider": "openai", "ticker": "__BATCH__"},
        tickers=[
            {"ticker": "NVDA", "position": 0, "status": "queued"},
            {"ticker": "AAPL", "position": 1, "status": "queued"},
        ],
    )
    assert isinstance(bid, uuid.UUID)

    with session_scope() as s:
        row = s.get(Batch, bid)
        assert row is not None
        assert row.status == "running"
        assert row.payload["base_selections"]["llm_provider"] == "openai"
        assert len(row.payload["tickers"]) == 2
        assert row.payload["tickers"][0]["ticker"] == "NVDA"


def test_insert_batch_returns_none_without_db(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.persistence.engine as eng

    monkeypatch.setitem(DEFAULT_CONFIG, "database_url", None)
    eng._reset_for_tests()

    from tradingagents.persistence.batches import insert_batch
    assert insert_batch({}, []) is None


def test_update_batch_status_typed_columns(db_engine):
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import insert_batch, update_batch_status

    bid = insert_batch({}, [{"ticker": "X", "position": 0, "status": "queued"}])
    paused = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    assert update_batch_status(
        bid, status="paused", paused_until=paused, cancel_requested=False,
    ) is True

    with session_scope() as s:
        row = s.get(Batch, bid)
        assert row.status == "paused"
        assert row.paused_until is not None

    # clear_paused_until=True sets it back to NULL.
    assert update_batch_status(bid, status="running", clear_paused_until=True) is True
    with session_scope() as s:
        row = s.get(Batch, bid)
        assert row.paused_until is None
        assert row.status == "running"


def test_update_batch_ticker_mutates_jsonb_slot(db_engine):
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import insert_batch, update_batch_ticker

    bid = insert_batch({}, [
        {"ticker": "AAA", "position": 0, "status": "queued", "attempts": 0},
        {"ticker": "BBB", "position": 1, "status": "queued", "attempts": 0},
    ])
    assert update_batch_ticker(
        bid, 1, status="running", attempts=1, job_id="abc123",
    ) is True

    with session_scope() as s:
        row = s.get(Batch, bid)
        # Position 0 untouched.
        assert row.payload["tickers"][0]["status"] == "queued"
        # Position 1 updated.
        assert row.payload["tickers"][1]["status"] == "running"
        assert row.payload["tickers"][1]["attempts"] == 1
        assert row.payload["tickers"][1]["job_id"] == "abc123"


def test_update_batch_ticker_out_of_range_returns_false(db_engine):
    from tradingagents.persistence.batches import insert_batch, update_batch_ticker

    bid = insert_batch({}, [{"ticker": "X", "position": 0, "status": "queued"}])
    assert update_batch_ticker(bid, 99, status="done") is False


def test_concurrent_update_batch_ticker_no_lost_writes(db_engine):
    """Two threads updating different ticker positions must both stick.

    Without the SELECT FOR UPDATE lock, one thread's read-modify-write
    on the JSONB payload would clobber the other. This test creates a
    race and verifies both updates land.
    """
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import insert_batch, update_batch_ticker

    bid = insert_batch({}, [
        {"ticker": f"T{i}", "position": i, "status": "queued"} for i in range(8)
    ])

    barrier = threading.Barrier(8)
    errors: list[Exception] = []

    def worker(position: int):
        try:
            barrier.wait()  # All threads attempt at the same time.
            update_batch_ticker(
                bid, position,
                status="running",
                job_id=f"job-{position}",
                attempts=1,
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors!r}"

    with session_scope() as s:
        row = s.get(Batch, bid)
        statuses = [t["status"] for t in row.payload["tickers"]]
        job_ids = [t.get("job_id") for t in row.payload["tickers"]]
        # All 8 updates must have stuck — no lost writes from JSONB races.
        assert statuses == ["running"] * 8
        assert job_ids == [f"job-{i}" for i in range(8)]


def test_list_batches_db_newest_first(db_engine):
    from tradingagents.persistence.batches import insert_batch, list_batches_db
    from tradingagents.persistence import Batch, session_scope

    b1 = insert_batch({}, [{"ticker": "AAA", "position": 0, "status": "done"}])
    b2 = insert_batch({}, [{"ticker": "BBB", "position": 0, "status": "running"}])
    b3 = insert_batch({}, [{"ticker": "CCC", "position": 0, "status": "queued"}])

    # The order they appear in the listing should be b3, b2, b1 — newest
    # submission first. Use second-level mtime so the ordering is stable
    # across fast inserts.
    rows = list_batches_db(limit=10)
    ids = [r.id for r in rows]
    assert ids[:3] == [str(b3), str(b2), str(b1)]


def test_recent_batch_row_exposes_template_surface(db_engine):
    """The DB-loaded view must expose `tickers`, `progress`,
    `status_label`, `total`, `done_count` like an in-memory BatchJob."""
    from tradingagents.persistence.batches import insert_batch, get_batch_db

    bid = insert_batch({}, [
        {"ticker": "AAA", "position": 0, "status": "done"},
        {"ticker": "BBB", "position": 1, "status": "running", "job_id": "x"},
        {"ticker": "CCC", "position": 2, "status": "queued"},
    ])
    row = get_batch_db(bid)
    assert row is not None
    assert row.total == 3
    assert row.done_count == 1
    assert row.progress == {"done": 1, "running": 1, "queued": 1}
    # Tickers expose the same attrs as BatchTicker.
    tickers = row.tickers
    assert [t.ticker for t in tickers] == ["AAA", "BBB", "CCC"]
    assert tickers[1].job_id == "x"
    assert tickers[1].status_label == "running"


def test_recent_batch_row_status_label_for_cancelled(db_engine):
    from tradingagents.persistence.batches import insert_batch, update_batch_status, get_batch_db

    bid = insert_batch({}, [{"ticker": "X", "position": 0, "status": "cancelled"}])
    update_batch_status(bid, status="cancelled",
                        completed_at=datetime.datetime.now(datetime.timezone.utc))
    row = get_batch_db(bid)
    assert row.status_label == "Stop"


def test_interrupt_in_flight_batches_marks_terminal(db_engine):
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import (
        insert_batch, interrupt_in_flight_batches, update_batch_status,
    )

    # 3 batches: running (insert default), paused, done (should not be touched).
    b1 = insert_batch({}, [{"ticker": "A", "position": 0, "status": "running"}])  # running
    b2 = insert_batch({}, [{"ticker": "B", "position": 0, "status": "running"}])
    update_batch_status(b2, status="paused",
                        paused_until=datetime.datetime.now(datetime.timezone.utc)
                                     + datetime.timedelta(hours=2))
    b3 = insert_batch({}, [{"ticker": "C", "position": 0, "status": "done"}])
    update_batch_status(b3, status="done",
                        completed_at=datetime.datetime.now(datetime.timezone.utc))

    n = interrupt_in_flight_batches()
    assert n == 2  # b1 + b2

    with session_scope() as s:
        # b1 + b2 errored, b3 untouched.
        assert s.get(Batch, b1).status == "error"
        assert s.get(Batch, b2).status == "error"
        assert s.get(Batch, b3).status == "done"
        # Per-ticker statuses were flipped from running → cancelled.
        b1_payload = s.get(Batch, b1).payload
        assert b1_payload["tickers"][0]["status"] == "cancelled"
        assert "interrupted" in b1_payload["tickers"][0].get("error", "").lower()
        assert "interrupted_note" in b1_payload


# ---------- BatchRegistry integration -------------------------------


@pytest.fixture
def mock_job_registry(monkeypatch):
    """Replace the per-ticker JobRegistry with a stub that immediately
    marks each job as done. Lets us test batch dispatch without the
    full LangGraph pipeline."""
    from webui.runner import JobRegistry, JobState
    from webui.models import RunSelections

    class _StubRegistry:
        def __init__(self):
            self._jobs = {}
            self._lock = threading.RLock()

        def start(self, selections: RunSelections):
            # Build a synthetic done JobState.
            job = JobState(
                id=uuid.uuid4().hex[:12],
                selections=selections,
                status="done",
                company_name=f"{selections.ticker} Inc.",
                decision="HOLD",
                final_state={"final_trade_decision": "HOLD"},
            )
            job.started_at = datetime.datetime.now()
            job.finished_at = datetime.datetime.now()
            with self._lock:
                self._jobs[job.id] = job
            return job

        def get(self, jid):
            return self._jobs.get(jid)

        def list_jobs(self):
            return list(self._jobs.values())

        def cancel(self, jid):
            return True

        def running_count(self):
            return 0

    stub = _StubRegistry()
    monkeypatch.setattr("webui.runner.get_registry", lambda: stub)
    return stub


def test_batch_registry_start_writes_db_row(db_engine, mock_job_registry):
    from tradingagents.persistence import Batch, session_scope
    from webui.batch import BatchRegistry
    from webui.models import RunSelections

    sel = RunSelections(
        ticker="__BATCH__", analysis_date=datetime.date.today().isoformat(),
        analysts=["market"], llm_provider="openai",
        quick_thinker="gpt-5.4-mini", deep_thinker="gpt-5.4",
    )
    reg = BatchRegistry()
    batch = reg.start(sel, ["NVDA", "AAPL", "MSFT"])
    assert batch.db_batch_id is not None

    # Wait for the dispatcher to finish (stub jobs are instant).
    for _ in range(50):
        if batch.status in ("done", "cancelled"):
            break
        time.sleep(0.1)
    assert batch.status in ("done", "cancelled")

    with session_scope() as s:
        row = s.get(Batch, batch.db_batch_id)
        assert row is not None
        assert row.status == batch.status
        assert row.completed_at is not None
        # Per-ticker statuses propagated.
        tickers = row.payload["tickers"]
        assert {t["ticker"] for t in tickers} == {"NVDA", "AAPL", "MSFT"}
        statuses = {t["status"] for t in tickers}
        assert statuses == {"done"}


def test_batch_registry_interrupts_existing_in_flight_on_construction(db_engine):
    """A new BatchRegistry sweeps the DB for any in-flight batches from a
    previous process and marks them errored."""
    from tradingagents.persistence import Batch, session_scope
    from tradingagents.persistence.batches import insert_batch
    from webui.batch import BatchRegistry

    bid = insert_batch({}, [{"ticker": "X", "position": 0, "status": "running"}])
    # Confirm it's currently 'running' (insert_batch default).
    with session_scope() as s:
        assert s.get(Batch, bid).status == "running"

    BatchRegistry()  # Constructor runs interrupt_in_flight_batches

    with session_scope() as s:
        row = s.get(Batch, bid)
        assert row.status == "error"
        assert "interrupted_note" in row.payload
