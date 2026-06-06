"""Tests for the recurring-schedule facility (`webui.schedules`) and the
grouped History helper (`webui.routes._group_history`).

Everything here runs in file-store mode with the batch registry stubbed
out — no Postgres, no LLM calls, no threads. The scheduler loop itself
is never started; `check_due(now=...)` is driven directly with frozen
clocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from webui.models import RunSelections
from webui.schedules import (
    SCHEDULE_TICKER_SENTINEL,
    Schedule,
    ScheduleRegistry,
    ScheduleStore,
    compute_next_run,
)


pytestmark = pytest.mark.unit


# Fixed offset so assertions don't depend on the host timezone.
TZ = timezone(timedelta(hours=-8))


def aware(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-06-01 is a Monday; 06-05 Friday, 06-06 Saturday, 06-08 Monday.


# ---------------------------------------------------------------------
# compute_next_run
# ---------------------------------------------------------------------


class TestComputeNextRun:
    def test_daily_before_time_fires_same_day(self):
        after = aware(2026, 6, 2, 6, 0)  # Tuesday 06:00
        assert compute_next_run("daily", "07:30", 0, after) == aware(2026, 6, 2, 7, 30)

    def test_daily_after_time_rolls_to_next_day(self):
        after = aware(2026, 6, 2, 8, 0)
        assert compute_next_run("daily", "07:30", 0, after) == aware(2026, 6, 3, 7, 30)

    def test_exact_boundary_is_strictly_after(self):
        after = aware(2026, 6, 2, 7, 30)
        assert compute_next_run("daily", "07:30", 0, after) == aware(2026, 6, 3, 7, 30)

    def test_weekdays_skips_weekend(self):
        after = aware(2026, 6, 5, 18, 0)  # Friday evening
        assert compute_next_run("weekdays", "07:00", 0, after) == aware(2026, 6, 8, 7, 0)

    def test_weekdays_saturday_lands_on_monday(self):
        after = aware(2026, 6, 6, 3, 0)  # Saturday
        assert compute_next_run("weekdays", "07:00", 0, after) == aware(2026, 6, 8, 7, 0)

    def test_weekly_same_day_after_time_waits_a_week(self):
        after = aware(2026, 6, 3, 8, 0)  # Wednesday 08:00, target Wed=2
        assert compute_next_run("weekly", "07:00", 2, after) == aware(2026, 6, 10, 7, 0)

    def test_weekly_before_target_day(self):
        after = aware(2026, 6, 1, 12, 0)  # Monday, target Thursday=3
        assert compute_next_run("weekly", "09:15", 3, after) == aware(2026, 6, 4, 9, 15)


# ---------------------------------------------------------------------
# Schedule model
# ---------------------------------------------------------------------


class TestScheduleModel:
    def test_tickers_normalized_and_deduped(self):
        s = _schedule(tickers=[" nvda", "NVDA", "aapl ", ""])
        assert s.tickers == ["NVDA", "AAPL"]

    def test_empty_tickers_rejected(self):
        with pytest.raises(ValueError):
            _schedule(tickers=["  ", ""])

    @pytest.mark.parametrize("bad", ["25:00", "07:99", "seven", ""])
    def test_bad_time_of_day_rejected(self, bad):
        with pytest.raises(ValueError):
            _schedule(time_of_day=bad)

    def test_time_of_day_zero_padded(self):
        assert _schedule(time_of_day="7:5").time_of_day == "07:05"

    def test_cadence_labels(self):
        assert _schedule(cadence="daily", time_of_day="07:00").cadence_label == "Daily at 07:00"
        assert _schedule(cadence="weekdays", time_of_day="06:30").cadence_label == "Weekdays at 06:30"
        assert (
            _schedule(cadence="weekly", weekday=4, time_of_day="16:00").cadence_label
            == "Fridays at 16:00"
        )

    def test_naive_timestamps_coerced_to_aware(self):
        s = _schedule()
        s2 = Schedule.model_validate(
            {**s.model_dump(mode="json"), "next_run_at": "2026-06-02T07:00:00"}
        )
        assert s2.next_run_at.tzinfo is not None

    def test_display_name_falls_back_to_tickers(self):
        assert _schedule(tickers=["NVDA", "AAPL"]).display_name == "NVDA, AAPL"
        assert _schedule(name="Watchlist").display_name == "Watchlist"


def _schedule(**overrides) -> Schedule:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tickers": ["NVDA"],
        "cadence": "daily",
        "time_of_day": "07:00",
        "selections": _selections().model_dump(),
    }
    base.update(overrides)
    return Schedule(**base)


def _selections() -> RunSelections:
    return RunSelections(
        ticker=SCHEDULE_TICKER_SENTINEL,
        analysis_date="2026-06-01",
        analysts=["market"],
        llm_provider="openai",
        quick_thinker="quick-model",
        deep_thinker="deep-model",
    )


# ---------------------------------------------------------------------
# Store (file mode) + registry
# ---------------------------------------------------------------------


class _StubBatchRegistry:
    def __init__(self):
        self.calls = []

    def start(self, selections, tickers):
        self.calls.append((selections, list(tickers)))
        return SimpleNamespace(id=f"batch-{len(self.calls)}")


@pytest.fixture(autouse=True)
def _openai_key_present(monkeypatch):
    """The fire-time key check reads OPENAI_API_KEY. conftest's
    placeholder only applies when the var is *unset*; a developer .env
    with `OPENAI_API_KEY=` (empty) would leak through and fail the
    firing tests. Pin it explicitly; the missing-key test delenvs."""
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")


@pytest.fixture
def file_store(tmp_path, monkeypatch):
    """ScheduleStore pinned to a temp file with the DB path disabled,
    so tests never touch the developer's real Postgres or JSON file."""
    monkeypatch.setattr(ScheduleStore, "_db_on", staticmethod(lambda: False))
    return ScheduleStore(path=tmp_path / "schedules.json")


@pytest.fixture
def stub_batches(monkeypatch):
    stub = _StubBatchRegistry()
    monkeypatch.setattr("webui.batch.get_batch_registry", lambda: stub)
    return stub


def _registry(file_store) -> ScheduleRegistry:
    return ScheduleRegistry(store=file_store)


class TestScheduleStore:
    def test_round_trip(self, file_store):
        reg = _registry(file_store)
        created = reg.create(
            name="Morning",
            tickers=["nvda", "AAPL"],
            cadence="weekdays",
            time_of_day="06:30",
            weekday=0,
            base_selections=_selections(),
        )
        loaded = ScheduleStore(path=file_store._path).load_all()
        assert len(loaded) == 1
        got = loaded[0]
        assert got.id == created.id
        assert got.tickers == ["NVDA", "AAPL"]
        assert got.cadence == "weekdays"
        assert got.next_run_at == created.next_run_at
        assert got.selections["llm_provider"] == "openai"

    def test_delete_persists(self, file_store):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        assert reg.delete(s.id) is True
        assert reg.delete(s.id) is False
        assert ScheduleStore(path=file_store._path).load_all() == []

    def test_missing_file_loads_empty(self, file_store):
        assert file_store.load_all() == []

    def test_backfills_missing_next_run(self, file_store):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        # Simulate a hand-edited file that lost next_run_at.
        s.next_run_at = None
        file_store._write_file([s])
        reloaded = _registry(ScheduleStore(path=file_store._path))
        got = reloaded.get(s.id)
        assert got is not None and got.next_run_at is not None


class TestScheduleRegistry:
    def test_create_sets_future_next_run(self, file_store):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        assert s.enabled
        assert s.next_run_at > datetime.now().astimezone()

    def test_not_due_does_not_fire(self, file_store, stub_batches):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        assert reg.check_due(now=s.next_run_at - timedelta(minutes=1)) == 0
        assert stub_batches.calls == []

    def test_due_fires_and_advances(self, file_store, stub_batches):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA", "AAPL"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        fire_at = s.next_run_at + timedelta(seconds=5)
        assert reg.check_due(now=fire_at) == 1

        assert len(stub_batches.calls) == 1
        selections, tickers = stub_batches.calls[0]
        assert tickers == ["NVDA", "AAPL"]
        assert selections.ticker == SCHEDULE_TICKER_SENTINEL
        # Analysis date is the fire date, not the creation-time placeholder.
        assert selections.analysis_date == fire_at.date().isoformat()

        assert s.last_batch_id == "batch-1"
        assert s.last_run_at == fire_at
        assert s.last_error is None
        assert s.next_run_at > fire_at

    def test_catch_up_fires_once(self, file_store, stub_batches):
        """A schedule overdue by days fires exactly once, then waits."""
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        now = datetime.now().astimezone()
        s.next_run_at = now - timedelta(days=3)

        assert reg.check_due(now=now) == 1
        assert len(stub_batches.calls) == 1
        assert s.next_run_at > now
        # Immediately re-checking must not double-fire.
        assert reg.check_due(now=now) == 0
        assert len(stub_batches.calls) == 1

    def test_missing_api_key_records_error_and_advances(
        self, file_store, stub_batches, monkeypatch,
    ):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        now = s.next_run_at + timedelta(seconds=1)
        assert reg.check_due(now=now) == 0
        assert stub_batches.calls == []
        assert "OPENAI_API_KEY" in (s.last_error or "")
        # Must advance anyway — a broken schedule can't retry every tick.
        assert s.next_run_at > now

    def test_disabled_schedule_never_fires(self, file_store, stub_batches):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        reg.set_enabled(s.id, False)
        assert s.next_run_at is None
        assert reg.check_due(now=datetime.now().astimezone() + timedelta(days=30)) == 0
        assert stub_batches.calls == []

    def test_reenable_recomputes_from_now(self, file_store):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        reg.set_enabled(s.id, False)
        got = reg.set_enabled(s.id, True)
        # No instant catch-up fire after a long pause.
        assert got.next_run_at > datetime.now().astimezone()

    def test_run_now_does_not_advance_cadence(self, file_store, stub_batches):
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        scheduled_next = s.next_run_at
        batch_id = reg.run_now(s.id)
        assert batch_id == "batch-1"
        assert len(stub_batches.calls) == 1
        assert s.next_run_at == scheduled_next
        assert s.last_batch_id == "batch-1"

    def test_run_now_unknown_id(self, file_store, stub_batches):
        reg = _registry(file_store)
        assert reg.run_now("nope") is None
        assert stub_batches.calls == []

    def test_survives_batch_submission_failure(self, file_store, monkeypatch):
        class _Boom:
            def start(self, *a, **k):
                raise RuntimeError("kaput")

        monkeypatch.setattr("webui.batch.get_batch_registry", lambda: _Boom())
        reg = _registry(file_store)
        s = reg.create(
            name="", tickers=["NVDA"], cadence="daily",
            time_of_day="07:00", weekday=0, base_selections=_selections(),
        )
        now = s.next_run_at + timedelta(seconds=1)
        assert reg.check_due(now=now) == 0
        assert "kaput" in (s.last_error or "")
        assert s.next_run_at > now


# ---------------------------------------------------------------------
# Grouped History helper
# ---------------------------------------------------------------------


class TestGroupHistory:
    def test_groups_by_ticker_preserving_recency_order(self):
        from webui.routes import _group_history

        # Newest-first input, like _partition_jobs produces. Mix of
        # RecentRunRow-shaped (typed .ticker) and JobState-shaped
        # (.selections.ticker) rows.
        nvda_new = SimpleNamespace(ticker="NVDA", company_name=None)
        aapl = SimpleNamespace(
            selections=SimpleNamespace(ticker="aapl"), company_name="Apple Inc.",
        )
        nvda_old = SimpleNamespace(
            selections=SimpleNamespace(ticker="NVDA"), company_name="NVIDIA Corp",
        )
        groups = _group_history([nvda_new, aapl, nvda_old])

        assert [g["ticker"] for g in groups] == ["NVDA", "AAPL"]
        nvda = groups[0]
        assert nvda["count"] == 2
        assert nvda["rows"] == [nvda_new, nvda_old]
        assert nvda["latest"] is nvda_new
        # First non-null company name in the group wins.
        assert nvda["company_name"] == "NVIDIA Corp"
        assert groups[1]["count"] == 1
        assert groups[1]["company_name"] == "Apple Inc."

    def test_dict_selections_fallback(self):
        from webui.routes import _group_history, _row_ticker

        row = SimpleNamespace(selections={"ticker": "msft"})
        assert _row_ticker(row) == "MSFT"
        assert _group_history([row])[0]["ticker"] == "MSFT"

    def test_empty_input(self):
        from webui.routes import _group_history

        assert _group_history([]) == []
