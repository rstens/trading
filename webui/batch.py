"""Batch submission: queue a list of tickers, run 3 at a time, pause 2 h on rate limit.

Each batch is dispatched in its own daemon thread that drives a
`ThreadPoolExecutor(max_workers=BATCH_MAX_PARALLEL)`. Each worker reuses
the existing `webui.runner.JobRegistry` to run one ticker — so every
ticker in a batch shows up as an ordinary job in `Recent jobs`, has its
own `/jobs/<id>` page, gets cached, and is cancellable individually.

Rate-limit detection: when a per-ticker job ends with `status="error"`
and the error text matches one of the well-known rate-limit / quota
signals, that ticker's status is flipped to `rate_limited`, it's
re-queued to the front of the pending deque, and the batch goes into a
`paused` state for `BATCH_RATE_LIMIT_PAUSE`. In-flight workers are
drained before the sleep so they don't keep hammering the API during
the pause window.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from webui.models import RunSelections
from webui.runner import get_registry as get_job_registry


log = logging.getLogger("tradingagents.webui.batch")


BATCH_MAX_PARALLEL = 3
BATCH_RATE_LIMIT_PAUSE = timedelta(hours=2)
BATCH_POLL_INTERVAL = 2           # seconds — per-ticker job status polling
BATCH_PAUSE_POLL_INTERVAL = 30    # seconds — pause-window wakeup cadence


# Substrings that strongly suggest a rate-limit / quota error, regardless
# of which provider's SDK raised it. Kept case-insensitive (lower()-ed
# before checking) and slightly redundant on purpose — different SDKs
# format the same condition differently.
_RATE_LIMIT_SIGNALS = (
    "ratelimiterror",
    "rate_limit",
    "rate limit",
    "rate-limit",
    " 429",
    "too many requests",
    "quota exceeded",
    "exceeded your",         # "exceeded your quota", "exceeded your rate limit"
    "exceeded the",          # "exceeded the rate limit"
    "insufficient_quota",
    "insufficient quota",
    "tokens per minute",
    "requests per minute",
    "throttle",
)


def _is_rate_limit_error(*texts: str) -> bool:
    blob = " ".join(t.lower() for t in texts if t)
    return any(s in blob for s in _RATE_LIMIT_SIGNALS)


@dataclass
class BatchTicker:
    """One ticker within a batch — wraps its lifecycle."""
    ticker: str
    # Index in the batch's `tickers` list — the JSONB array slot the
    # DAO mutates. Stable for the lifetime of the batch (we never
    # reorder).
    position: int = 0
    # queued | running | done | error | cancelled | rate_limited
    status: str = "queued"
    job_id: Optional[str] = None
    # Durable runs.id for this ticker's job. The "open" link prefers it
    # over the ephemeral job_id so it survives a server restart.
    db_run_id: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    company_name: Optional[str] = None

    @property
    def status_label(self) -> str:
        """UI-facing label — mirrors JobState.status_label for cancelled."""
        return {
            "cancelled": "Stop",
            "rate_limited": "Rate-limited (will retry)",
        }.get(self.status, self.status)


@dataclass
class BatchJob:
    id: str
    base_selections: RunSelections
    tickers: List[BatchTicker]
    submitted_at: datetime
    # queued | running | paused | done | cancelled
    status: str = "queued"
    paused_until: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancel_requested: bool = False
    # Postgres UUID linking this in-memory batch to its persisted row.
    # None when DB is unconfigured — every persist call no-ops.
    db_batch_id: Optional[Any] = None

    @property
    def total(self) -> int:
        return len(self.tickers)

    @property
    def done_count(self) -> int:
        return sum(1 for t in self.tickers if t.status == "done")

    @property
    def progress(self) -> Dict[str, int]:
        """Counts grouped by ticker status. UI uses this for the summary line."""
        counts: Dict[str, int] = {}
        for t in self.tickers:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    @property
    def status_label(self) -> str:
        if self.status == "cancelled":
            return "Stop"
        if self.status == "paused" and self.paused_until:
            return f"Paused (until {self.paused_until.strftime('%H:%M:%S')})"
        return self.status


class BatchRegistry:
    """Thread-safe in-memory registry of batches.

    Phase 6 addition: on construction, calls
    `interrupt_in_flight_batches()` so any batches that were active when
    the previous process died are marked terminal in the DB (status=error,
    payload note explaining the restart). v1 doesn't replay the deque —
    the user re-submits the ticker list to resume.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._batches: Dict[str, BatchJob] = {}
        # Best-effort restart recovery. No-op when DB is unconfigured.
        try:
            from tradingagents.persistence.batches import interrupt_in_flight_batches
            n = interrupt_in_flight_batches()
            if n:
                log.info("Marked %d in-flight batch(es) as interrupted "
                         "(previous process exited mid-flight)", n)
        except Exception as e:  # noqa: BLE001
            log.warning("Batch restart-recovery sweep failed: %s", e)

    def get(self, batch_id: str) -> Optional[BatchJob]:
        with self.lock:
            return self._batches.get(batch_id)

    def list_batches(self) -> List[BatchJob]:
        with self.lock:
            return sorted(
                self._batches.values(),
                key=lambda b: b.submitted_at,
                reverse=True,
            )

    def start(self, base_selections: RunSelections, tickers: List[str]) -> BatchJob:
        # Normalize + dedupe while preserving order. Assign positions
        # so the DB-side per-ticker updater knows which JSONB slot to
        # mutate.
        seen: set[str] = set()
        clean: List[BatchTicker] = []
        for raw in tickers:
            t = raw.strip().upper()
            if not t or t in seen:
                continue
            seen.add(t)
            clean.append(BatchTicker(ticker=t, position=len(clean)))
        if not clean:
            raise ValueError("No tickers provided.")

        batch = BatchJob(
            id=uuid.uuid4().hex[:12],
            base_selections=base_selections,
            tickers=clean,
            submitted_at=datetime.now(),
        )

        # Persist the batch to Postgres (no-op when DB is unconfigured).
        try:
            from tradingagents.persistence.batches import insert_batch
            batch.db_batch_id = insert_batch(
                base_selections=base_selections.model_dump(),
                tickers=[self._ticker_to_payload(t) for t in clean],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Batch DB insert failed: %s", e)

        with self.lock:
            self._batches[batch.id] = batch

        threading.Thread(
            target=self._run_batch, args=(batch.id,), daemon=True,
            name=f"batch-{batch.id[:6]}-dispatcher",
        ).start()
        return batch

    # ----- DB-persistence helpers (Phase 6) -----

    @staticmethod
    def _ticker_to_payload(bt: BatchTicker) -> Dict[str, Any]:
        """Serialize a BatchTicker for the JSONB array slot."""
        return {
            "ticker": bt.ticker,
            "position": bt.position,
            "status": bt.status,
            "attempts": bt.attempts,
            "job_id": bt.job_id,
            "db_run_id": bt.db_run_id,
            "error": bt.error,
            "company_name": bt.company_name,
        }

    def _persist_ticker(self, batch: BatchJob, bt: BatchTicker) -> None:
        """Push the current state of `bt` into the persisted JSONB."""
        if not batch.db_batch_id:
            return
        try:
            from tradingagents.persistence.batches import update_batch_ticker
            update_batch_ticker(
                batch.db_batch_id,
                bt.position,
                status=bt.status,
                attempts=bt.attempts,
                job_id=bt.job_id,
                db_run_id=bt.db_run_id,
                error=bt.error,
                company_name=bt.company_name,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Batch ticker persist failed for %s/%s: %s",
                        batch.id, bt.ticker, e)

    def _persist_status(self, batch: BatchJob, **fields: Any) -> None:
        """Push batch-level state to the typed columns."""
        if not batch.db_batch_id:
            return
        try:
            from tradingagents.persistence.batches import update_batch_status
            update_batch_status(batch.db_batch_id, **fields)
        except Exception as e:  # noqa: BLE001
            log.warning("Batch status persist failed for %s: %s",
                        batch.id, e)

    def cancel(self, batch_id: str) -> bool:
        """Request cancellation. Returns False if unknown / already terminal."""
        with self.lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return False
            if batch.status in ("done", "cancelled"):
                return False
            batch.cancel_requested = True
            # Snapshot per-ticker job IDs to cancel below (outside the lock).
            running_ids = [t.job_id for t in batch.tickers
                           if t.status == "running" and t.job_id]
        self._persist_status(batch, cancel_requested=True)
        job_reg = get_job_registry()
        for jid in running_ids:
            job_reg.cancel(jid)
        return True

    # ------- internals --------

    def _run_batch(self, batch_id: str) -> None:
        batch = self._batches[batch_id]
        log.info("[batch %s] starting with %d tickers", batch.id, batch.total)
        with self.lock:
            batch.status = "running"
        self._persist_status(batch, status="running")

        pending: deque[BatchTicker] = deque(batch.tickers)
        active: Dict[concurrent.futures.Future, BatchTicker] = {}

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=BATCH_MAX_PARALLEL,
            thread_name_prefix=f"batch-{batch.id[:6]}",
        )
        try:
            while pending or active:
                # 1. Cancellation — drain in-flight, mark remaining queued
                #    tickers as cancelled, then exit.
                if batch.cancel_requested:
                    if active:
                        log.info("[batch %s] cancel: draining %d in-flight",
                                 batch.id, len(active))
                        concurrent.futures.wait(
                            active.keys(),
                            return_when=concurrent.futures.ALL_COMPLETED,
                        )
                        for fut, bt in list(active.items()):
                            self._absorb_completed(batch, fut, bt, pending)
                        active.clear()
                    for bt in pending:
                        if bt.status == "queued":
                            bt.status = "cancelled"
                            self._persist_ticker(batch, bt)
                    pending.clear()
                    break

                # 2. Pause window — drain in-flight, sleep until expiry.
                if batch.paused_until and datetime.now() < batch.paused_until:
                    if active:
                        concurrent.futures.wait(
                            active.keys(),
                            return_when=concurrent.futures.ALL_COMPLETED,
                        )
                        for fut, bt in list(active.items()):
                            self._absorb_completed(batch, fut, bt, pending)
                        active.clear()
                    log.info("[batch %s] paused until %s",
                             batch.id, batch.paused_until)
                    while True:
                        resumed = False
                        with self.lock:
                            if batch.cancel_requested:
                                break
                            if (batch.paused_until is None
                                    or datetime.now() >= batch.paused_until):
                                batch.paused_until = None
                                batch.status = "running"
                                resumed = True
                                break
                        time.sleep(BATCH_PAUSE_POLL_INTERVAL)
                    if resumed:
                        self._persist_status(
                            batch, status="running", clear_paused_until=True,
                        )
                    continue  # re-enter loop top (handles cancel-during-pause)

                # 3. Fill up to BATCH_MAX_PARALLEL in-flight.
                while pending and len(active) < BATCH_MAX_PARALLEL:
                    bt = pending.popleft()
                    bt.attempts += 1
                    self._persist_ticker(batch, bt)
                    fut = executor.submit(self._run_ticker, batch, bt)
                    active[fut] = bt

                if not active:
                    break

                # 4. Wait for at least one in-flight to finish.
                done, _ = concurrent.futures.wait(
                    active.keys(),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    bt = active.pop(fut)
                    self._absorb_completed(batch, fut, bt, pending)
        finally:
            executor.shutdown(wait=True)
            with self.lock:
                if batch.cancel_requested:
                    batch.status = "cancelled"
                else:
                    batch.status = "done"
                batch.completed_at = datetime.now()
            self._persist_status(
                batch, status=batch.status, completed_at=batch.completed_at,
            )
            log.info("[batch %s] terminal: %s", batch.id, batch.status)

    def _absorb_completed(
        self,
        batch: BatchJob,
        future: concurrent.futures.Future,
        bt: BatchTicker,
        pending: deque,
    ) -> None:
        """Inspect a completed ticker. If rate-limited, re-queue + arm pause."""
        try:
            future.result()  # surfaces any unhandled exception from _run_ticker
        except Exception as e:  # noqa: BLE001
            log.exception("[batch %s] worker fault on %s: %s",
                          batch.id, bt.ticker, e)
            bt.status = "error"
            bt.error = str(e)

        if bt.status == "rate_limited":
            pause_armed = False
            with self.lock:
                if batch.paused_until is None:
                    batch.paused_until = datetime.now() + BATCH_RATE_LIMIT_PAUSE
                    batch.status = "paused"
                    pause_armed = True
                pending.appendleft(bt)
                bt.status = "queued"
            if pause_armed:
                self._persist_status(
                    batch, status="paused", paused_until=batch.paused_until,
                )
            self._persist_ticker(batch, bt)
            log.warning(
                "[batch %s] rate-limited on %s — pause until %s",
                batch.id, bt.ticker, batch.paused_until,
            )
        else:
            # Final state of this ticker — push terminal status / error.
            self._persist_ticker(batch, bt)

    def _run_ticker(self, batch: BatchJob, bt: BatchTicker) -> None:
        """One ticker — spawn a JobState, poll for terminal status, classify.

        Final-state writes happen in `_absorb_completed` (one persist
        per ticker on the dispatcher thread, simpler than racing
        per-field updates from the worker thread). The mid-run writes
        below capture the `running` + `job_id` transition so the UI
        sees the per-ticker `open` link as soon as the inner job spawns.
        """
        if batch.cancel_requested:
            bt.status = "cancelled"
            # Final state — persist now; the dispatcher's
            # _absorb_completed won't see this path on a fresh deque.
            self._persist_ticker(batch, bt)
            return

        # Per-ticker selections: clone the base, swap in this ticker.
        selections = batch.base_selections.model_copy(update={"ticker": bt.ticker})

        job_reg = get_job_registry()
        try:
            job = job_reg.start(selections)
        except Exception as e:  # noqa: BLE001
            bt.status = "error"
            bt.error = f"Failed to start job: {e}"
            self._persist_ticker(batch, bt)
            return
        bt.job_id = job.id
        # db_run_id is stamped on the job thread after insert_run, so it
        # may not be set yet at this instant — best-effort here, captured
        # reliably after the poll loop below.
        bt.db_run_id = str(job.db_run_id) if getattr(job, "db_run_id", None) else None
        bt.status = "running"
        self._persist_ticker(batch, bt)

        # Poll until the inner job reaches a terminal state.
        cancel_relayed = False
        while True:
            if batch.cancel_requested and not cancel_relayed:
                job_reg.cancel(job.id)
                cancel_relayed = True
            if job.status in ("done", "error", "cancelled"):
                break
            time.sleep(BATCH_POLL_INTERVAL)

        bt.company_name = getattr(job, "company_name", None)
        # By now the inner job has run insert_run; capture the durable id.
        if getattr(job, "db_run_id", None):
            bt.db_run_id = str(job.db_run_id)

        if job.status == "error" and _is_rate_limit_error(
            job.error or "", job.traceback or "",
        ):
            bt.status = "rate_limited"
            bt.error = job.error
            # _absorb_completed handles persistence for the rate-limited path
            # (it both re-queues and arms the pause atomically).
            return

        bt.status = job.status
        bt.error = job.error
        # _absorb_completed persists the terminal state after this returns.


# Module-global registry. FastAPI's Depends() resolves through get_batch_registry.
_batch_registry: Optional[BatchRegistry] = None


def get_batch_registry() -> BatchRegistry:
    global _batch_registry
    if _batch_registry is None:
        _batch_registry = BatchRegistry()
    return _batch_registry
