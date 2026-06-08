"""DAO for the `batches` table.

Schema decision #6 puts the per-ticker rows inside a JSONB payload
(`payload["tickers"] = [...]`) rather than a separate normalised table.
That's fine for typical batch sizes (≤ 200) and makes the table
self-contained, but means **per-ticker state changes need a row lock**
when the dispatcher runs ticker workers in parallel
(BATCH_MAX_PARALLEL = 3). `update_batch_ticker` issues
`SELECT … FOR UPDATE` on the parent row before reading/rewriting the
JSONB so two concurrent workers can't drop each other's writes.

All functions are no-ops when the DB is unconfigured and swallow their
own exceptions — a Postgres outage degrades to "in-memory only".
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from tradingagents.persistence.models import Batch
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.batches")


@dataclass
class PersistedBatchTicker:
    """Lightweight ticker view used by the DB-loaded batch detail page.

    Mirrors `webui.batch.BatchTicker`'s template-facing attribute
    surface (`ticker`, `status`, `status_label`, `job_id`, `error`,
    `attempts`, `company_name`, `position`) so the existing
    `partials/batch_status.html` renders it without branching.
    """
    ticker: str
    status: str = "queued"
    job_id: Optional[str] = None
    db_run_id: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    company_name: Optional[str] = None
    position: int = 0

    @property
    def status_label(self) -> str:
        return {
            "cancelled": "Stop",
            "rate_limited": "Rate-limited (will retry)",
        }.get(self.status, self.status)


@dataclass
class RecentBatchRow:
    """Display-only view of a `Batch` for the Recent batches table and
    the batch-detail page after restart.

    Surface lines up with `BatchJob` (id, status, status_label,
    submitted_at, paused_until, total, done_count, tickers, progress)
    so the templates don't branch on source.
    """
    id: str
    status: str
    submitted_at: Optional[datetime.datetime]
    completed_at: Optional[datetime.datetime]
    paused_until: Optional[datetime.datetime]
    cancel_requested: bool
    total: int
    done_count: int
    payload: Dict[str, Any] = field(default_factory=dict)
    db_batch_id: Optional[uuid.UUID] = None

    @property
    def status_label(self) -> str:
        if self.status == "cancelled":
            return "Stop"
        if self.status == "paused" and self.paused_until:
            return f"Paused (until {self.paused_until.strftime('%H:%M:%S')})"
        return self.status

    @property
    def tickers(self) -> List[PersistedBatchTicker]:
        """Iterate the JSONB tickers list as typed ticker views."""
        out: List[PersistedBatchTicker] = []
        for t in (self.payload.get("tickers") or []):
            if not isinstance(t, dict):
                continue
            out.append(PersistedBatchTicker(
                ticker=t.get("ticker", "?"),
                status=t.get("status") or "queued",
                job_id=t.get("job_id"),
                db_run_id=t.get("db_run_id"),
                error=t.get("error"),
                attempts=int(t.get("attempts") or 0),
                company_name=t.get("company_name"),
                position=int(t.get("position") or 0),
            ))
        return out

    @property
    def progress(self) -> Dict[str, int]:
        """Counts grouped by ticker status — used for the summary line."""
        counts: Dict[str, int] = {}
        for t in self.tickers:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts


# ---------- write path ------------------------------------------------


def insert_batch(
    base_selections: Dict[str, Any],
    tickers: List[Dict[str, Any]],
) -> Optional[uuid.UUID]:
    """Insert a new `batches` row, return its UUID.

    `tickers` should be a list of dicts mirroring the BatchTicker fields
    (ticker, position, status, attempts, job_id, error, company_name)
    — the dispatcher writes these in order.
    """
    try:
        with session_scope() as s:
            if s is None:
                return None
            batch = Batch(
                status="running",
                payload={
                    "base_selections": base_selections or {},
                    "tickers": list(tickers or []),
                },
            )
            s.add(batch)
            s.flush()
            return batch.id
    except Exception as e:  # noqa: BLE001
        log.warning("insert_batch failed: %s", e)
        return None


def update_batch_status(
    batch_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    paused_until: Optional[datetime.datetime] = None,
    clear_paused_until: bool = False,
    completed_at: Optional[datetime.datetime] = None,
    cancel_requested: Optional[bool] = None,
) -> bool:
    """Update the typed columns. None args skipped; pass
    ``clear_paused_until=True`` to set `paused_until` back to NULL."""
    if batch_id is None:
        return False
    try:
        with session_scope() as s:
            if s is None:
                return False
            batch = s.get(Batch, batch_id)
            if batch is None:
                log.warning("update_batch_status: batch %s not found", batch_id)
                return False
            if status is not None:
                batch.status = status
            if paused_until is not None:
                batch.paused_until = paused_until
            elif clear_paused_until:
                batch.paused_until = None
            if completed_at is not None:
                batch.completed_at = completed_at
            if cancel_requested is not None:
                batch.cancel_requested = cancel_requested
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("update_batch_status failed for %s: %s", batch_id, e)
        return False


def update_batch_ticker(
    batch_id: uuid.UUID,
    position: int,
    **field_updates: Any,
) -> bool:
    """Update one entry in `payload["tickers"]` under a row lock.

    The lock is critical: BatchRegistry runs up to BATCH_MAX_PARALLEL
    ticker workers concurrently, each calling this for their own
    position. Without `SELECT … FOR UPDATE` two transactions reading
    the same JSONB blob would race — last write wins, the other's
    update vanishes.
    """
    if batch_id is None:
        return False
    try:
        with session_scope() as s:
            if s is None:
                return False
            batch = s.execute(
                select(Batch).where(Batch.id == batch_id).with_for_update()
            ).scalar_one_or_none()
            if batch is None:
                return False
            payload = dict(batch.payload or {})
            tickers = list(payload.get("tickers") or [])
            if not (0 <= position < len(tickers)):
                log.warning(
                    "update_batch_ticker: position %d out of range for batch %s (len=%d)",
                    position, batch_id, len(tickers),
                )
                return False
            entry = dict(tickers[position] or {})
            entry.update(field_updates)
            tickers[position] = entry
            payload["tickers"] = tickers
            batch.payload = payload
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("update_batch_ticker failed for %s/%d: %s",
                    batch_id, position, e)
        return False


# ---------- read path -------------------------------------------------


def get_batch_db(batch_id: uuid.UUID) -> Optional[RecentBatchRow]:
    """Single batch by id, formatted for the detail page."""
    try:
        with session_scope() as s:
            if s is None:
                return None
            batch = s.get(Batch, batch_id)
            if batch is None:
                return None
            return _to_recent_row(batch)
    except Exception as e:  # noqa: BLE001
        log.warning("get_batch_db failed for %s: %s", batch_id, e)
        return None


def list_batches_db(limit: int = 20) -> List[RecentBatchRow]:
    """All batches, newest-submitted-first."""
    try:
        with session_scope() as s:
            if s is None:
                return []
            stmt = (
                select(Batch)
                .order_by(Batch.submitted_at.desc())
                .limit(limit)
            )
            rows = s.execute(stmt).scalars().all()
            return [_to_recent_row(b) for b in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("list_batches_db failed: %s", e)
        return []


# ---------- restart recovery ------------------------------------------


def interrupt_in_flight_batches() -> int:
    """Mark any non-terminal batch as `error` with a payload note.

    Called once at process startup (via `BatchRegistry.__init__`). A v2
    follow-up could replay the deque from the persisted state; for now
    we just mark the batch failed so the user knows to re-submit.
    """
    try:
        with session_scope() as s:
            if s is None:
                return 0
            rows = s.execute(
                select(Batch).where(
                    Batch.status.in_(("queued", "running", "paused"))
                )
            ).scalars().all()
            now = datetime.datetime.now(datetime.timezone.utc)
            for b in rows:
                payload = dict(b.payload or {})
                payload["interrupted_at"] = now.isoformat()
                payload["interrupted_note"] = (
                    "Server restarted while this batch was in flight. "
                    "Re-submit the ticker list to resume."
                )
                # Mark each non-terminal ticker as cancelled in the JSONB
                # so the detail page renders coherently.
                tickers = list(payload.get("tickers") or [])
                for i, t in enumerate(tickers):
                    if t.get("status") in ("queued", "running"):
                        t = dict(t)
                        t["status"] = "cancelled"
                        t.setdefault("error", "interrupted by server restart")
                        tickers[i] = t
                payload["tickers"] = tickers
                b.payload = payload
                b.status = "error"
                b.completed_at = now
            return len(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("interrupt_in_flight_batches failed: %s", e)
        return 0


# ---------- helpers ---------------------------------------------------


def _to_recent_row(b: Batch) -> RecentBatchRow:
    payload = b.payload or {}
    tickers = payload.get("tickers") or []
    done = sum(1 for t in tickers if isinstance(t, dict) and t.get("status") == "done")
    return RecentBatchRow(
        id=str(b.id),
        status=b.status,
        submitted_at=b.submitted_at,
        completed_at=b.completed_at,
        paused_until=b.paused_until,
        cancel_requested=bool(b.cancel_requested),
        total=len(tickers),
        done_count=done,
        payload=payload,
        db_batch_id=b.id,
    )
