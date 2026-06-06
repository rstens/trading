"""DAO for the `schedules` table.

The in-memory `webui.schedules.ScheduleRegistry` owns schedule state
while the process runs; these helpers mirror it durably so schedules
survive restarts. Unlike runs/batches (where the DB is canonical for
terminal history), the registry loads *everything* at startup and is
authoritative thereafter — the DB is a pure write-through store.

All functions are no-ops when the DB is unconfigured and swallow their
own exceptions — a Postgres outage degrades to "schedule changes don't
persist this session". `upsert_schedule_db` returns False on failure so
the caller (`ScheduleStore`) can fall back to the JSON file, mirroring
the preferences-module contract.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from tradingagents.persistence.models import Schedule
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.schedules")


def upsert_schedule_db(
    schedule_id: uuid.UUID,
    *,
    enabled: bool,
    next_run_at: Optional[datetime.datetime],
    last_run_at: Optional[datetime.datetime],
    created_at: Optional[datetime.datetime],
    payload: Dict[str, Any],
) -> bool:
    """Insert or update one schedule row. Returns True on success."""
    if schedule_id is None:
        return False
    try:
        with session_scope() as s:
            if s is None:
                return False
            row = s.get(Schedule, schedule_id)
            if row is None:
                row = Schedule(id=schedule_id)
                # Let Postgres default created_at unless the caller has
                # one (file-mode schedules imported after enabling DB).
                if created_at is not None:
                    row.created_at = created_at
                s.add(row)
            row.enabled = enabled
            row.next_run_at = next_run_at
            row.last_run_at = last_run_at
            row.payload = payload or {}
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("upsert_schedule_db failed for %s: %s", schedule_id, e)
        return False


def delete_schedule_db(schedule_id: uuid.UUID) -> bool:
    """Delete one schedule row. Returns True when a row was removed."""
    if schedule_id is None:
        return False
    try:
        with session_scope() as s:
            if s is None:
                return False
            row = s.get(Schedule, schedule_id)
            if row is None:
                return False
            s.delete(row)
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("delete_schedule_db failed for %s: %s", schedule_id, e)
        return False


def list_schedules_db() -> Optional[List[Dict[str, Any]]]:
    """All schedule rows as plain dicts (registry hydrates them into
    `webui.schedules.Schedule` models).

    Returns None when the DB is unconfigured / unreachable — distinct
    from [] (DB reachable, zero schedules) so the caller knows whether
    to fall back to the JSON file.
    """
    try:
        with session_scope() as s:
            if s is None:
                return None
            rows = s.execute(
                select(Schedule).order_by(Schedule.created_at.asc())
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "enabled": r.enabled,
                    "next_run_at": (
                        r.next_run_at.isoformat() if r.next_run_at else None
                    ),
                    "last_run_at": (
                        r.last_run_at.isoformat() if r.last_run_at else None
                    ),
                    "created_at": (
                        r.created_at.isoformat() if r.created_at else None
                    ),
                    **(r.payload or {}),
                }
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001
        log.warning("list_schedules_db failed: %s", e)
        return None
