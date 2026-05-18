"""Per-call session scope context manager.

Usage:

    from tradingagents.persistence import session_scope

    with session_scope() as s:
        if s is None:
            return  # no DB configured — caller's no-op fallback
        s.add(some_row)
        # commit happens on clean exit; rollback on exception.

Critical contract: every persistence call site MUST handle the
``None`` case. The web UI / batch / runner / decision-log paths all
fall through to in-memory / file-based fallbacks when DB is unset.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy.orm import Session

from tradingagents.persistence.engine import get_session_factory


@contextmanager
def session_scope() -> Generator[Optional[Session], None, None]:
    """Yield a Session (committed on success, rolled back on exception),
    or yield None if no DB is configured.
    """
    factory = get_session_factory()
    if factory is None:
        yield None
        return

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
