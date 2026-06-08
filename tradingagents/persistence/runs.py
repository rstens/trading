"""DAO helpers for the `runs` table family — write at start, finalize at
terminal, query for cache hits and Recent jobs.

Every function is a no-op when the DB is unconfigured (``session_scope``
yields None) and never propagates exceptions — a Postgres outage
degrades gracefully to "no persistence happened this run" rather than
failing the analysis. Callers don't need to check return values to
decide whether to keep going.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, func, or_, select

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.persistence.models import (
    Run,
    RunReport,
    RunTokenUsage,
)
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.runs")

# How long a failed run stays referenced in the History / Recent listing.
# After this it's filtered out of the UI (the DB row is kept for audit).
ERROR_RUN_TTL = datetime.timedelta(hours=24)


# ----- Write path ------------------------------------------------------


def insert_run(
    ticker: str,
    analysis_date: str,
    selections: Dict[str, Any],
    *,
    batch_id: Optional[uuid.UUID] = None,
    company_name: Optional[str] = None,
) -> Optional[uuid.UUID]:
    """Insert a new ``runs`` row at job start. Returns the row's UUID, or
    None if no DB is configured / the write failed.
    """
    try:
        with session_scope() as s:
            if s is None:
                return None
            run = Run(
                ticker=ticker,
                analysis_date=datetime.date.fromisoformat(analysis_date),
                status="running",
                selections=selections or {},
                batch_id=batch_id,
                company_name=company_name,
            )
            s.add(run)
            s.flush()  # populate the UUID default before returning
            return run.id
    except Exception as e:  # noqa: BLE001
        log.warning("insert_run failed for %s/%s: %s", ticker, analysis_date, e)
        return None


def update_company_name(run_id: uuid.UUID, company_name: Optional[str]) -> None:
    """Patch in the resolved company name once yfinance lookup completes."""
    if not run_id or not company_name:
        return
    try:
        with session_scope() as s:
            if s is None:
                return
            run = s.get(Run, run_id)
            if run is not None:
                run.company_name = company_name
    except Exception as e:  # noqa: BLE001
        log.warning("update_company_name failed for %s: %s", run_id, e)


def persist_run_completion(
    run_id: uuid.UUID,
    *,
    status: str,
    decision: Optional[str],
    error: Optional[str],
    traceback: Optional[str],
    raw_state: Optional[Dict[str, Any]],
    partial_state: Optional[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
    company_name: Optional[str] = None,
) -> bool:
    """Finalize a run in a single transaction.

    Updates the parent ``runs`` row, inserts one ``run_reports`` row per
    non-empty section in ``partial_state``, and upserts a single
    ``run_token_usage`` row. Returns True on success, False on
    no-DB / failure.
    """
    if not run_id:
        return False
    try:
        with session_scope() as s:
            if s is None:
                return False
            run = s.get(Run, run_id)
            if run is None:
                log.warning("persist_run_completion: run %s not found", run_id)
                return False

            run.status = status
            run.finished_at = datetime.datetime.now(datetime.timezone.utc)
            if decision is not None:
                run.decision = decision
                # parse_rating returns None when no rating is detectable.
                run.rating = parse_rating(decision) or None
            if error is not None:
                run.error = error
            if traceback is not None:
                run.traceback = traceback
            if raw_state is not None:
                run.raw_state = raw_state
            if company_name is not None:
                run.company_name = company_name

            # One row per populated section. Strings only (no debate
            # state dicts) — those have already been flattened to
            # bull_history / aggressive_history / etc. in the runner.
            for section, content in (partial_state or {}).items():
                if isinstance(content, str) and content.strip():
                    s.add(RunReport(run_id=run_id, section=section, content=content))

            # Upsert via merge so a retry doesn't error on duplicate PK.
            if usage is not None:
                s.merge(RunTokenUsage(run_id=run_id, usage=usage))

        return True
    except Exception as e:  # noqa: BLE001
        log.warning("persist_run_completion failed for %s: %s", run_id, e)
        return False


# ----- Read path -------------------------------------------------------


@dataclass
class RecentRunRow:
    """Display-only view of a `Run` for the Recent jobs table.

    Shape lines up with what the index.html template reads from a
    JobState (``j.id``, ``j.selections.ticker``, ``j.status``,
    ``j.status_label``, ``j.started_at``, ``j.decision``), so the
    template doesn't need to branch on the source.
    """
    id: str
    ticker: str
    analysis_date: str
    status: str
    started_at: Optional[datetime.datetime]
    finished_at: Optional[datetime.datetime]
    decision: Optional[str]
    rating: Optional[str]
    selections: Dict[str, Any] = field(default_factory=dict)
    company_name: Optional[str] = None
    db_run_id: Optional[uuid.UUID] = None

    @property
    def status_label(self) -> str:
        """Mirrors JobState.status_label — cancelled → 'Stop'."""
        return "Stop" if self.status == "cancelled" else self.status


def find_cache_hit_db(
    ticker: str,
    analysis_date: str,
    max_age_seconds: int = 7 * 24 * 3600,
) -> Optional[Dict[str, Any]]:
    """Return the most recent successful run for ``(ticker, date)`` within
    the TTL, formatted as a dict the runner can hand to
    ``_populate_from_cache``. Returns None on no DB / no hit / failure.
    """
    try:
        with session_scope() as s:
            if s is None:
                return None
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                seconds=max_age_seconds,
            )
            stmt = (
                select(Run)
                .where(Run.ticker == ticker.strip().upper())
                .where(Run.analysis_date == datetime.date.fromisoformat(analysis_date))
                .where(Run.status == "done")
                .where(Run.finished_at >= cutoff)
                .order_by(Run.finished_at.desc())
                .limit(1)
            )
            run = s.execute(stmt).scalar_one_or_none()
            if run is None:
                return None
            reports = {r.section: r.content for r in run.reports}
            return {
                "run_id": run.id,
                "ticker": run.ticker,
                "analysis_date": run.analysis_date.isoformat(),
                "company_name": run.company_name,
                "selections": run.selections,
                "decision": run.decision,
                "rating": run.rating,
                "final_state": run.raw_state,
                "partial_state": reports,
                "finished_at": run.finished_at,
            }
    except Exception as e:  # noqa: BLE001
        log.warning("find_cache_hit_db failed for %s/%s: %s", ticker, analysis_date, e)
        return None


def list_recent_terminal_runs(limit: int = 40) -> List[RecentRunRow]:
    """Recent done / error / cancelled runs, newest first.

    Returns dataclass rows (not ORM objects) so callers can iterate
    after session close without lazy-load surprises. The default limit
    is intentionally larger than the UI's 20-row display so the hybrid
    merge (in-memory wins) still has 20 left over after dedupe.
    """
    try:
        with session_scope() as s:
            if s is None:
                return []
            # Error runs are dropped from the listing once they're older
            # than ERROR_RUN_TTL — a failed run is only useful to look at
            # for a short window, and (post-restart reconciliation) they
            # would otherwise pile up in History indefinitely. The rows
            # stay in the DB for audit; they're just no longer referenced
            # by the UI. `done` / `cancelled` are always listed.
            cutoff = datetime.datetime.now(datetime.timezone.utc) - ERROR_RUN_TTL
            stmt = (
                select(Run)
                .where(
                    or_(
                        Run.status.in_(("done", "cancelled")),
                        and_(
                            Run.status == "error",
                            func.coalesce(Run.finished_at, Run.started_at) >= cutoff,
                        ),
                    )
                )
                .order_by(Run.started_at.desc())
                .limit(limit)
            )
            rows = s.execute(stmt).scalars().all()
            return [
                RecentRunRow(
                    id=str(r.id),
                    ticker=r.ticker,
                    analysis_date=r.analysis_date.isoformat(),
                    status=r.status,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    decision=r.decision,
                    rating=r.rating,
                    selections=r.selections or {},
                    company_name=r.company_name,
                    db_run_id=r.id,
                )
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001
        log.warning("list_recent_terminal_runs failed: %s", e)
        return []


def interrupt_in_flight_runs() -> int:
    """Mark any non-terminal `runs` row as `error` — restart recovery.

    A run that was `queued`/`running` when the process died can never
    resolve itself: the in-memory `JobState` that owned it is gone, so
    the row would otherwise sit at `running` forever and the detail page
    would poll it indefinitely. Called once at web startup so orphaned
    runs surface a terminal status sourced from the DB. Returns the count
    reconciled. No-op + swallow on DB-off, mirroring the module contract.
    """
    try:
        with session_scope() as s:
            if s is None:
                return 0
            rows = s.execute(
                select(Run).where(Run.status.in_(("queued", "running")))
            ).scalars().all()
            now = datetime.datetime.now(datetime.timezone.utc)
            for run in rows:
                run.status = "error"
                run.error = "Server restarted while this run was in flight."
                run.finished_at = now
            if rows:
                log.info("Reconciled %d in-flight run(s) as interrupted", len(rows))
            return len(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("interrupt_in_flight_runs failed: %s", e)
        return 0
