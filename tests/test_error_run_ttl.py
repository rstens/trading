"""Error runs are dropped from the History/Recent listing after 24h.

The DB-side filter (`list_recent_terminal_runs`) is exercised only with a
test database; here we cover the in-memory `_partition_jobs` path and the
TTL contract, which run without a DB.
"""

from datetime import datetime, timedelta, timezone

import pytest

from webui.models import RunSelections
from webui.routes import _partition_jobs
from webui.runner import JobRegistry, JobState

pytestmark = pytest.mark.unit


def _job(job_id, status, started_at):
    return JobState(
        id=job_id,
        selections=RunSelections(
            ticker="NVDA", analysis_date="2026-06-06", analysts=["market"],
            llm_provider="anthropic", quick_thinker="claude-haiku-4-5",
            deep_thinker="claude-sonnet-4-6",
        ),
        status=status,
        started_at=started_at,
    )


def _all_ids(buckets):
    active, terminal_today, older = buckets
    return {j.id for j in (*active, *terminal_today, *older)}


class TestErrorRunTtl:
    def test_ttl_is_24h(self):
        from tradingagents.persistence.runs import ERROR_RUN_TTL
        assert ERROR_RUN_TTL == timedelta(hours=24)

    def test_old_error_run_is_dropped(self):
        now = datetime.now(timezone.utc)
        reg = JobRegistry()
        reg._jobs["fresh-err"] = _job("fresh-err", "error", now - timedelta(hours=1))
        reg._jobs["stale-err"] = _job("stale-err", "error", now - timedelta(hours=25))
        ids = _all_ids(_partition_jobs(reg))
        assert "fresh-err" in ids          # within TTL → still shown
        assert "stale-err" not in ids      # past TTL → dropped

    def test_done_and_cancelled_never_dropped(self):
        now = datetime.now(timezone.utc)
        reg = JobRegistry()
        reg._jobs["old-done"] = _job("old-done", "done", now - timedelta(days=30))
        reg._jobs["old-cancel"] = _job("old-cancel", "cancelled", now - timedelta(days=30))
        ids = _all_ids(_partition_jobs(reg))
        assert "old-done" in ids
        assert "old-cancel" in ids

    def test_active_runs_unaffected_by_ttl(self):
        # A running job has no terminal status; it must always appear even
        # if (hypothetically) its started_at is old.
        now = datetime.now(timezone.utc)
        reg = JobRegistry()
        reg._jobs["run"] = _job("run", "running", now - timedelta(hours=48))
        active, _, _ = _partition_jobs(reg)
        assert {j.id for j in active} == {"run"}

    def test_boundary_just_under_24h_kept(self):
        now = datetime.now(timezone.utc)
        reg = JobRegistry()
        reg._jobs["edge"] = _job("edge", "error", now - timedelta(hours=23, minutes=59))
        assert "edge" in _all_ids(_partition_jobs(reg))
