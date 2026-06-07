"""Regression tests for JobRegistry.list_jobs timestamp sorting.

The crash this guards against: cache-hydrated jobs carry tz-aware UTC
``started_at`` while live runs historically used naive ``datetime.now()``,
and ``sorted()`` over the mix raised
``TypeError: can't compare offset-naive and offset-aware datetimes``.
"""

from datetime import datetime, timezone

import pytest

from webui.models import RunSelections
from webui.runner import JobRegistry, JobState, _epoch

pytestmark = pytest.mark.unit


def _job(job_id, started_at):
    return JobState(
        id=job_id,
        selections=RunSelections(
            ticker="NVDA", analysis_date="2026-06-06", analysts=["market"],
            llm_provider="anthropic", quick_thinker="claude-haiku-4-5",
            deep_thinker="claude-sonnet-4-6",
        ),
        status="done",
        started_at=started_at,
    )


class TestListJobsSorting:
    def test_mixed_naive_and_aware_does_not_crash(self):
        reg = JobRegistry()
        # aware (cache-hydration path) + naive (legacy live-run path) + None
        reg._jobs["aware"] = _job("aware", datetime(2026, 6, 6, 10, tzinfo=timezone.utc))
        reg._jobs["naive"] = _job("naive", datetime(2026, 6, 6, 12))
        reg._jobs["none"] = _job("none", None)

        jobs = reg.list_jobs()  # must not raise
        assert len(jobs) == 3
        # Newest first; the None-timestamp job sorts last.
        assert jobs[-1].id == "none"

    def test_orders_by_epoch_descending(self):
        reg = JobRegistry()
        reg._jobs["older"] = _job("older", datetime(2026, 6, 6, 9, tzinfo=timezone.utc))
        # 11:00 local — convert to compare; make it unambiguously later in UTC
        reg._jobs["newer"] = _job("newer", datetime(2026, 6, 6, 23, tzinfo=timezone.utc))
        order = [j.id for j in reg.list_jobs()]
        assert order == ["newer", "older"]


class TestEpochHelper:
    def test_naive_and_aware_are_comparable_floats(self):
        a = _epoch(datetime(2026, 6, 6, 10, tzinfo=timezone.utc))
        n = _epoch(datetime(2026, 6, 6, 10))
        assert isinstance(a, float) and isinstance(n, float)

    def test_none_sorts_last(self):
        assert _epoch(None) == float("-inf")
