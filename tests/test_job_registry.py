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


def _job(job_id, started_at, db_run_id=None):
    return JobState(
        id=job_id,
        selections=RunSelections(
            ticker="NVDA", analysis_date="2026-06-06", analysts=["market"],
            llm_provider="anthropic", quick_thinker="claude-haiku-4-5",
            deep_thinker="claude-sonnet-4-6",
        ),
        status="done",
        started_at=started_at,
        db_run_id=db_run_id,
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


class TestGetByDbRunId:
    def test_finds_live_job_by_durable_run_id(self):
        reg = JobRegistry()
        # Registry is keyed by the ephemeral JobState.id, NOT the runs.id.
        job = _job("ephemeral-jobstate-id", datetime.now(timezone.utc),
                   db_run_id="019ea489-e816-7a11-8172-efd622c6ad6a")
        reg._jobs[job.id] = job
        # Looking up by the durable runs.id must still find the live job.
        assert reg.get_by_db_run_id("019ea489-e816-7a11-8172-efd622c6ad6a") is job
        # And by the ephemeral id via the normal getter.
        assert reg.get("ephemeral-jobstate-id") is job

    def test_unknown_and_falsy_ids_return_none(self):
        reg = JobRegistry()
        reg._jobs["x"] = _job("x", datetime.now(timezone.utc), db_run_id="abc")
        assert reg.get_by_db_run_id("nope") is None
        assert reg.get_by_db_run_id("") is None
        assert reg.get_by_db_run_id(None) is None

    def test_ignores_jobs_without_db_run_id(self):
        reg = JobRegistry()
        reg._jobs["x"] = _job("x", datetime.now(timezone.utc), db_run_id=None)
        assert reg.get_by_db_run_id("anything") is None


class TestJobDetailResolution:
    """The reported bug: the durable /jobs/{db_run_id} link 404'd because
    the route only checked the registry by JobState.id and the DB by
    runs.id — never the registry by db_run_id."""

    def _client_with_job(self, job):
        from fastapi.testclient import TestClient
        from webui.app import create_app
        from webui.runner import get_registry
        get_registry()._jobs[job.id] = job
        return TestClient(create_app())

    def test_open_link_by_db_run_id_resolves_live_job(self):
        run_id = "019ea489-7076-7641-8d19-e33cb914f77d"
        job = _job("ephemeral-id-1", datetime.now(timezone.utc), db_run_id=run_id)
        try:
            client = self._client_with_job(job)
            # The durable link uses db_run_id, which is not the registry key.
            r = client.get(f"/jobs/{run_id}")
            assert r.status_code == 200
            # And the polled status fragment resolves it too.
            assert client.get(f"/htmx/jobs/{run_id}/status").status_code == 200
        finally:
            from webui.runner import get_registry
            get_registry()._jobs.pop(job.id, None)

    def test_truly_unknown_id_still_404s(self):
        from fastapi.testclient import TestClient
        from webui.app import create_app
        client = TestClient(create_app())
        r = client.get("/jobs/019ea489-0000-7000-8000-000000000000")
        assert r.status_code == 404


class TestInterruptInFlightRunsNoDb:
    def test_no_db_returns_zero_and_does_not_raise(self, monkeypatch):
        # With no DATABASE_URL, session_scope yields None → no-op.
        from tradingagents.persistence import interrupt_in_flight_runs
        assert interrupt_in_flight_runs() == 0

    def test_exported_from_package(self):
        import tradingagents.persistence as p
        assert hasattr(p, "interrupt_in_flight_runs")
