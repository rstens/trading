"""Recurring unattended analyses: schedule a ticker list, fire on cadence.

A `Schedule` couples a ticker list + base `RunSelections` with a cadence
(daily / weekdays / weekly at a time of day, server-local). The
`ScheduleRegistry` keeps every schedule in memory, runs a daemon ticker
thread that wakes every `SCHEDULE_POLL_INTERVAL` seconds, and fires due
schedules by submitting a normal batch through `webui.batch
.BatchRegistry` — so each scheduled run gets the full batch treatment
(3-way parallelism, rate-limit pause, per-ticker jobs in Recent jobs,
cancellation) and lands in the same history as manual runs.

Catch-up semantics: `next_run_at` is persisted, and the due check is
simply `next_run_at <= now` — so a schedule that was due while the
server was down fires once at startup (with *today's* analysis date),
then advances to its next regular slot. At most one catch-up batch per
schedule, regardless of how long the server was off.

Storage follows the project's two-mode convention:

* **DB on** (``TRADINGAGENTS_DATABASE_URL``) — write-through to the
  ``schedules`` table; the JSON file is neither read nor written.
* **DB off** — atomic JSON file at ``~/.tradingagents/webui_schedules
  .json`` (same directory + tempfile/replace pattern as
  ``webui_last_settings.json``).

Times: schedules are defined in server-local wall-clock time ("06:30"
means 06:30 where the server runs). Internally every timestamp is an
*aware* local datetime (``datetime.now().astimezone()``) so comparisons
against DB-loaded timestamptz values are always safe.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid as uuid_lib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.uuid7 import uuid7
from webui.models import RunSelections


log = logging.getLogger("tradingagents.webui.schedules")


SCHEDULE_POLL_INTERVAL = 30  # seconds between due-schedule checks

# Sentinel ticker stored in a schedule's base selections — the batch
# dispatcher overrides it per real ticker, same trick as /api/batches.
SCHEDULE_TICKER_SENTINEL = "__SCHEDULE__"

_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)

Cadence = Literal["daily", "weekdays", "weekly"]


def _now() -> datetime:
    """Aware local now — the single clock the scheduler reasons in."""
    return datetime.now().astimezone()


def compute_next_run(
    cadence: str,
    time_of_day: str,
    weekday: int,
    after: datetime,
) -> datetime:
    """First occurrence of the schedule strictly after `after`.

    `after` must be aware; the result carries the same tzinfo. DST
    transitions can shift the wall-clock fire time by the offset delta
    for that one day — acceptable for a "run my analysis each morning"
    facility.
    """
    hour, minute = (int(p) for p in time_of_day.split(":", 1))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    if cadence == "weekdays":
        while candidate.weekday() > 4:  # 5=Sat, 6=Sun
            candidate += timedelta(days=1)
    elif cadence == "weekly":
        while candidate.weekday() != weekday:
            candidate += timedelta(days=1)
    return candidate


class Schedule(BaseModel):
    """One recurring analysis definition."""

    id: str
    name: str = ""
    tickers: List[str] = Field(min_length=1)
    cadence: Cadence = "daily"
    time_of_day: str = "07:00"  # "HH:MM", server-local
    weekday: int = Field(default=0, ge=0, le=6)  # Monday=0; weekly only
    enabled: bool = True
    # Base RunSelections dump. `ticker` holds the sentinel and
    # `analysis_date` is overridden with the fire date at submission.
    selections: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_batch_id: Optional[str] = None
    last_error: Optional[str] = None

    @field_validator("created_at", "next_run_at", "last_run_at")
    @classmethod
    def _ensure_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Naive timestamps (hand-edited JSON) are treated as local time.

        Keeps every comparison in the scheduler aware-vs-aware so a
        stray naive value can't TypeError the due check.
        """
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.astimezone()
        return v

    @field_validator("time_of_day")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        try:
            hour, minute = (int(p) for p in v.split(":", 1))
        except (ValueError, AttributeError):
            raise ValueError(f"time_of_day must be HH:MM, got {v!r}")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"time_of_day out of range: {v!r}")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("tickers")
    @classmethod
    def _normalize_tickers(cls, v: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for raw in v:
            t = (raw or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        if not out:
            raise ValueError("Provide at least one ticker.")
        return out

    @property
    def display_name(self) -> str:
        return self.name or ", ".join(self.tickers[:4]) + (
            "…" if len(self.tickers) > 4 else ""
        )

    @property
    def cadence_label(self) -> str:
        if self.cadence == "daily":
            return f"Daily at {self.time_of_day}"
        if self.cadence == "weekdays":
            return f"Weekdays at {self.time_of_day}"
        return f"{_WEEKDAY_NAMES[self.weekday]}s at {self.time_of_day}"

    def compute_next(self, after: Optional[datetime] = None) -> datetime:
        return compute_next_run(
            self.cadence, self.time_of_day, self.weekday, after or _now(),
        )


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

# Sibling of webui_last_settings.json — see webui.preferences.
_FILE = Path(DEFAULT_CONFIG["data_cache_dir"]).parent / "webui_schedules.json"


class ScheduleStore:
    """Write-through persistence for schedules.

    DB mode mirrors each mutation to the `schedules` table; file mode
    rewrites the JSON file (atomic tempfile + replace). The registry is
    the in-memory source of truth either way, so reads only happen once
    at startup via `load_all()`.
    """

    def __init__(self, path: Path = _FILE) -> None:
        self._path = path

    # -- helpers --

    @staticmethod
    def _db_on() -> bool:
        try:
            from tradingagents.persistence.decisions import db_is_configured
            return db_is_configured()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _schedule_uuid(schedule: Schedule) -> Optional[uuid_lib.UUID]:
        try:
            return uuid_lib.UUID(schedule.id)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _payload(schedule: Schedule) -> Dict[str, Any]:
        """JSONB payload — everything except the typed columns."""
        return {
            "name": schedule.name,
            "tickers": list(schedule.tickers),
            "cadence": schedule.cadence,
            "time_of_day": schedule.time_of_day,
            "weekday": schedule.weekday,
            "selections": dict(schedule.selections),
            "last_batch_id": schedule.last_batch_id,
            "last_error": schedule.last_error,
        }

    # -- API --

    def load_all(self) -> List[Schedule]:
        """All persisted schedules. DB wins when configured; the JSON
        file is only consulted when the DB is off/unreachable."""
        if self._db_on():
            try:
                from tradingagents.persistence.schedules import list_schedules_db
                rows = list_schedules_db()
                if rows is not None:
                    return self._hydrate(rows)
                log.warning("DB schedule load failed — falling back to file")
            except Exception as e:  # noqa: BLE001
                log.warning("DB schedule load unavailable (%s) — falling back to file", e)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return self._hydrate(raw if isinstance(raw, list) else [])
        except (FileNotFoundError, OSError, ValueError):
            return []

    @staticmethod
    def _hydrate(rows: List[Dict[str, Any]]) -> List[Schedule]:
        out: List[Schedule] = []
        for row in rows:
            try:
                out.append(Schedule.model_validate(row))
            except Exception as e:  # noqa: BLE001
                log.warning("Skipping malformed schedule row %s: %s",
                            (row or {}).get("id"), e)
        return out

    def upsert(self, schedule: Schedule, all_schedules: List[Schedule]) -> None:
        """Persist one changed schedule. `all_schedules` is the registry's
        full current list — needed by file mode, which rewrites the file."""
        if self._db_on():
            sid = self._schedule_uuid(schedule)
            if sid is not None:
                from tradingagents.persistence.schedules import upsert_schedule_db
                if upsert_schedule_db(
                    sid,
                    enabled=schedule.enabled,
                    next_run_at=schedule.next_run_at,
                    last_run_at=schedule.last_run_at,
                    created_at=schedule.created_at,
                    payload=self._payload(schedule),
                ):
                    return  # DB write is canonical; skip the file mirror
            log.warning("DB schedule upsert failed for %s — writing file fallback",
                        schedule.id)
        self._write_file(all_schedules)

    def delete(self, schedule: Schedule, all_schedules: List[Schedule]) -> None:
        """Remove one schedule. `all_schedules` is the post-removal list."""
        if self._db_on():
            sid = self._schedule_uuid(schedule)
            if sid is not None:
                from tradingagents.persistence.schedules import delete_schedule_db
                if delete_schedule_db(sid):
                    return
            log.warning("DB schedule delete failed for %s — writing file fallback",
                        schedule.id)
        self._write_file(all_schedules)

    def _write_file(self, schedules: List[Schedule]) -> None:
        """Atomic rewrite of the whole JSON file (small N, simple)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [s.model_dump(mode="json") for s in schedules]
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=".webui_schedules_", suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise


# ---------------------------------------------------------------------
# Registry + scheduler loop
# ---------------------------------------------------------------------


class ScheduleRegistry:
    """Thread-safe registry of schedules + the daemon loop that fires them.

    The ticker thread is NOT started in __init__ — call
    `ensure_ticker_started()` (done once from the FastAPI lifespan) so
    that merely importing/constructing the registry in tests doesn't
    spin a background thread against the developer's real schedule file.
    """

    def __init__(self, store: Optional[ScheduleStore] = None) -> None:
        self.lock = threading.RLock()
        self._store = store or ScheduleStore()
        self._schedules: Dict[str, Schedule] = {
            s.id: s for s in self._store.load_all()
        }
        self._ticker_started = False
        # Backfill next_run_at for enabled schedules that lost it
        # (malformed file edit, downgrade, ...). Disabled ones stay None.
        for s in self._schedules.values():
            if s.enabled and s.next_run_at is None:
                s.next_run_at = s.compute_next()
                self._store.upsert(s, list(self._schedules.values()))
        if self._schedules:
            log.info("Loaded %d schedule(s)", len(self._schedules))

    # -- thread management --

    def ensure_ticker_started(self) -> None:
        """Idempotently start the scheduler loop."""
        with self.lock:
            if self._ticker_started:
                return
            self._ticker_started = True
        threading.Thread(
            target=self._loop, daemon=True, name="schedule-ticker",
        ).start()
        log.info("Schedule ticker started (poll every %ds)", SCHEDULE_POLL_INTERVAL)

    def _loop(self) -> None:
        while True:
            try:
                self.check_due()
            except Exception:  # noqa: BLE001
                # The loop must survive anything — a single bad schedule
                # or a transient store failure shouldn't kill scheduling
                # for the rest of the process lifetime.
                log.exception("Schedule check failed")
            time.sleep(SCHEDULE_POLL_INTERVAL)

    # -- queries --

    def list_schedules(self) -> List[Schedule]:
        with self.lock:
            return sorted(
                self._schedules.values(), key=lambda s: s.created_at,
            )

    def get(self, schedule_id: str) -> Optional[Schedule]:
        with self.lock:
            return self._schedules.get(schedule_id)

    # -- mutations --

    def create(
        self,
        *,
        name: str,
        tickers: List[str],
        cadence: str,
        time_of_day: str,
        weekday: int,
        base_selections: RunSelections,
    ) -> Schedule:
        schedule = Schedule(
            # Time-ordered v7 (same convention as runs/batches PKs) so
            # DB-mode inserts append to the B-tree.
            id=str(uuid7()),
            name=(name or "").strip(),
            tickers=tickers,
            cadence=cadence,
            time_of_day=time_of_day,
            weekday=weekday,
            selections=base_selections.model_dump(),
        )
        schedule.next_run_at = schedule.compute_next()
        with self.lock:
            self._schedules[schedule.id] = schedule
            self._store.upsert(schedule, list(self._schedules.values()))
        log.info("Schedule %s created: %s (%s) — next run %s",
                 schedule.id[:8], schedule.display_name,
                 schedule.cadence_label, schedule.next_run_at)
        return schedule

    def set_enabled(self, schedule_id: str, enabled: bool) -> Optional[Schedule]:
        """Enable/disable. Re-enabling recomputes next_run_at from *now*
        so a long-disabled schedule doesn't instantly catch-up-fire."""
        with self.lock:
            schedule = self._schedules.get(schedule_id)
            if schedule is None:
                return None
            schedule.enabled = enabled
            schedule.next_run_at = schedule.compute_next() if enabled else None
            self._store.upsert(schedule, list(self._schedules.values()))
        return schedule

    def delete(self, schedule_id: str) -> bool:
        with self.lock:
            schedule = self._schedules.pop(schedule_id, None)
            if schedule is None:
                return False
            self._store.delete(schedule, list(self._schedules.values()))
        return True

    def run_now(self, schedule_id: str) -> Optional[str]:
        """Fire immediately (extra run — the regular cadence is not
        advanced). Returns the new batch id, or None on failure."""
        with self.lock:
            schedule = self._schedules.get(schedule_id)
        if schedule is None:
            return None
        return self._fire(schedule, advance=False)

    # -- scheduler core --

    def check_due(self, now: Optional[datetime] = None) -> int:
        """Fire every enabled schedule whose next_run_at has passed.
        Returns the number fired. Called by the ticker thread; callable
        directly (with a frozen `now`) from tests."""
        now = now or _now()
        with self.lock:
            due = [
                s for s in self._schedules.values()
                if s.enabled and s.next_run_at is not None and s.next_run_at <= now
            ]
        fired = 0
        for schedule in due:
            if self._fire(schedule, advance=True, now=now) is not None:
                fired += 1
        return fired

    def _fire(
        self,
        schedule: Schedule,
        *,
        advance: bool,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Submit one batch for `schedule`. Advances next_run_at when
        `advance` (regular cadence fire) regardless of success — a
        broken schedule must not retry every poll tick."""
        now = now or _now()
        batch_id: Optional[str] = None
        error: Optional[str] = None

        try:
            selections = RunSelections.model_validate({
                **schedule.selections,
                "ticker": SCHEDULE_TICKER_SENTINEL,
                "analysis_date": now.date().isoformat(),
            })
        except Exception as e:  # noqa: BLE001
            selections = None
            error = f"Invalid stored selections: {e}"

        if selections is not None:
            # API-key presence check at fire time — the key may have
            # been removed since the schedule was created.
            from tradingagents.llm_clients.api_key_env import get_api_key_env
            env_var = get_api_key_env(selections.llm_provider)
            if env_var and not os.environ.get(env_var):
                error = f"{env_var} is not set — skipped this run."

        if error is None and selections is not None:
            try:
                from webui.batch import get_batch_registry
                batch = get_batch_registry().start(selections, list(schedule.tickers))
                batch_id = batch.id
                log.info("Schedule %s fired → batch %s (%s)",
                         schedule.id[:8], batch.id, ", ".join(schedule.tickers))
            except Exception as e:  # noqa: BLE001
                error = f"Batch submission failed: {e}"

        if error:
            log.warning("Schedule %s fire failed: %s", schedule.id[:8], error)

        with self.lock:
            if schedule.id not in self._schedules:
                # Deleted while the batch was being submitted — don't
                # resurrect the row by persisting post-fire state.
                return batch_id
            schedule.last_run_at = now
            schedule.last_error = error
            if batch_id is not None:
                schedule.last_batch_id = batch_id
            if advance:
                schedule.next_run_at = schedule.compute_next(after=now)
            self._store.upsert(schedule, list(self._schedules.values()))
        return batch_id


# Module-global registry. FastAPI's Depends() resolves through
# get_schedule_registry; the lifespan hook starts the ticker thread.
_schedule_registry: Optional[ScheduleRegistry] = None


def get_schedule_registry() -> ScheduleRegistry:
    global _schedule_registry
    if _schedule_registry is None:
        _schedule_registry = ScheduleRegistry()
    return _schedule_registry
