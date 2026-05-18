"""DAO for the decision log — Postgres replacement for
`~/.tradingagents/memory/trading_memory.md`.

Mirrors the `TradingMemoryLog` API surface so the existing caller in
`graph/trading_graph.py::_resolve_pending_entries` and the memory wrapper
both keep working transparently. Each function is a no-op when the DB
is unconfigured (`session_scope` yields None); the markdown log remains
the active store in that mode.

When the DB IS configured, markdown writes are **sunset** per decision
#2 — the markdown file (if still present from before persistence was
wired in) is left as a frozen historical artifact and `TradingMemoryLog`
neither reads nor writes it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.persistence.engine import get_engine
from tradingagents.persistence.models import Decision
from tradingagents.persistence.session import session_scope


log = logging.getLogger("tradingagents.persistence.decisions")


def db_is_configured() -> bool:
    """True when ``TRADINGAGENTS_DATABASE_URL`` resolves to a usable engine."""
    return get_engine() is not None


# ---------- write path -------------------------------------------------


def store_pending_decision(
    ticker: str,
    trade_date: str,
    decision_text: str,
) -> bool:
    """Insert a `pending` decision row. Idempotent on (ticker, trade_date).

    Returns True on success or idempotent no-op; False when DB is off or
    the write failed. Callers can treat False as "DB write didn't
    happen, decide whether to fall back".
    """
    try:
        with session_scope() as s:
            if s is None:
                return False
            d = _coerce_date(trade_date)
            existing = s.execute(
                select(Decision)
                .where(Decision.ticker == ticker)
                .where(Decision.trade_date == d)
            ).scalar_one_or_none()
            if existing is not None:
                return True  # idempotent — duplicate insert avoided

            rating = parse_rating(decision_text)
            s.add(Decision(
                ticker=ticker,
                trade_date=d,
                status="pending",
                payload={
                    "rating": rating,
                    "decision_text": decision_text,
                },
            ))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("store_pending_decision failed for %s/%s: %s",
                    ticker, trade_date, e)
        return False


def resolve_decisions(updates: Iterable[Dict[str, Any]]) -> int:
    """Mark a batch of pending decisions as resolved.

    Each ``updates`` item must carry ``ticker``, ``trade_date``,
    ``raw_return``, ``alpha_return``, ``holding_days``, ``reflection``.
    Optional: ``benchmark``. Returns the count actually resolved.
    """
    count = 0
    try:
        with session_scope() as s:
            if s is None:
                return 0
            now = datetime.datetime.now(datetime.timezone.utc)
            for upd in updates:
                d = _coerce_date(upd["trade_date"])
                row = s.execute(
                    select(Decision)
                    .where(Decision.ticker == upd["ticker"])
                    .where(Decision.trade_date == d)
                    .where(Decision.status == "pending")
                ).scalar_one_or_none()
                if row is None:
                    continue

                payload = dict(row.payload or {})
                payload["raw_return"] = upd.get("raw_return")
                payload["alpha_return"] = upd.get("alpha_return")
                payload["holding_days"] = upd.get("holding_days")
                payload["reflection"] = upd.get("reflection")
                if "benchmark" in upd:
                    payload["benchmark"] = upd["benchmark"]
                row.payload = payload
                row.status = "resolved"
                row.resolved_at = now
                count += 1
        return count
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_decisions failed: %s", e)
        return 0


# ---------- read path --------------------------------------------------


def get_pending_decisions(ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pending entries (status=pending), optionally filtered to a ticker.

    Returns dicts in the same shape as
    ``TradingMemoryLog._parse_entry`` produces, so callers don't need
    to branch on the source.
    """
    try:
        with session_scope() as s:
            if s is None:
                return []
            stmt = select(Decision).where(Decision.status == "pending")
            if ticker:
                stmt = stmt.where(Decision.ticker == ticker)
            rows = s.execute(stmt.order_by(Decision.trade_date)).scalars().all()
            return [_to_legacy_dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("get_pending_decisions failed: %s", e)
        return []


def get_past_context_db(
    ticker: str,
    n_same: int = 5,
    n_cross: int = 3,
    n_vector: int = 5,
    company_name: Optional[str] = None,
    analysis_date: Optional[str] = None,
    vector_min_threshold: int = 5,
) -> str:
    """Build the agent-prompt context string from the persistence layer.

    Composes three blocks (in order):
      1. **Same-ticker recent decisions** — resolved entries from the
         `decisions` table, most-recent-first (`n_same`).
      2. **Cross-ticker reflections** — what we've learned from other
         tickers (`n_cross`).
      3. **Semantically similar past analyses** (Phase 5 addition) —
         vector hits from `run_embeddings.summary` ordered by cosine
         distance from a deterministic seed string built from the
         current run. Skipped entirely until the vector store has at
         least `vector_min_threshold` rows (avoids RAG noise during
         early operation).

    Returns "" when DB is unavailable, the query fails, or nothing
    matches — caller (the Portfolio Manager prompt) handles the empty
    case gracefully (the `Lessons from prior decisions...` line is
    omitted).
    """
    try:
        with session_scope() as s:
            if s is None:
                return ""
            same = s.execute(
                select(Decision)
                .where(Decision.status == "resolved")
                .where(Decision.ticker == ticker)
                .order_by(Decision.resolved_at.desc())
                .limit(n_same)
            ).scalars().all()
            cross = s.execute(
                select(Decision)
                .where(Decision.status == "resolved")
                .where(Decision.ticker != ticker)
                .order_by(Decision.resolved_at.desc())
                .limit(n_cross)
            ).scalars().all()
    except Exception as e:  # noqa: BLE001
        log.warning("get_past_context_db (decisions) failed: %s", e)
        return ""

    parts = []
    if same:
        parts.append(f"Past analyses of {ticker} (most recent first):")
        parts.extend(_format_full(d) for d in same)
    if cross:
        parts.append("Recent cross-ticker lessons:")
        parts.extend(_format_reflection_only(d) for d in cross)

    # Phase 5: RAG injection. Lives in its own try/except so an embedding
    # API outage doesn't strip the decision-log context the prompt has
    # always had.
    try:
        rag_block = _build_rag_block(
            ticker=ticker,
            company_name=company_name,
            analysis_date=analysis_date,
            n_vector=n_vector,
            vector_min_threshold=vector_min_threshold,
        )
        if rag_block:
            parts.append(rag_block)
    except Exception as e:  # noqa: BLE001
        log.warning("get_past_context_db (RAG) failed: %s", e)

    return "\n\n".join(parts) if parts else ""


def _build_rag_block(
    ticker: str,
    company_name: Optional[str],
    analysis_date: Optional[str],
    n_vector: int,
    vector_min_threshold: int,
) -> str:
    """Return the formatted "Semantically similar past analyses:" block,
    or "" if we don't have enough vector data yet."""
    # Local import — keeps decisions.py importable in environments
    # where the embedding provider's SDK isn't installed.
    from tradingagents.persistence.embeddings import count_embeddings, find_similar

    if count_embeddings() < vector_min_threshold:
        return ""

    seed = _build_seed(ticker, company_name, analysis_date)
    hits = find_similar(
        query_text=seed,
        sections=["summary"],     # the post-pipeline synthesis section
        limit=n_vector,
    )
    if not hits:
        return ""

    lines = ["Semantically similar past analyses (vector retrieval):"]
    for h in hits:
        excerpt = (h.content or "").strip()
        if len(excerpt) > 400:
            excerpt = excerpt[:400].rstrip() + "..."
        # Distance is in [0, 2] for cosine; print as 0.xx for legibility.
        lines.append(
            f"[{h.ticker} · {h.section} · cos={h.distance:.2f}]\n{excerpt}"
        )
    return "\n\n".join(lines)


def _build_seed(
    ticker: str,
    company_name: Optional[str],
    analysis_date: Optional[str],
) -> str:
    """Deterministic short query string for vector retrieval.

    Per the Phase 5 plan: keep this short and predictable. Avoids an
    extra LLM round-trip to manufacture a richer query; vector
    similarity does the heavy lifting against the embedded summary.
    """
    bits = [f"Trading analysis for {ticker}"]
    if company_name:
        bits[-1] = f"Trading analysis for {ticker} ({company_name})"
    if analysis_date:
        bits.append(f"on {analysis_date}")
    return " ".join(bits)


# ---------- formatting + coercion helpers ------------------------------


def _coerce_date(value) -> datetime.date:
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    return datetime.date.fromisoformat(str(value))


def _fmt_pct(v) -> Optional[str]:
    if isinstance(v, (int, float)):
        return f"{v:+.1%}"
    return None


def _to_legacy_dict(d: Decision) -> Dict[str, Any]:
    """Match the TradingMemoryLog._parse_entry dict shape."""
    p = d.payload or {}
    return {
        "date": d.trade_date.isoformat(),
        "ticker": d.ticker,
        "rating": p.get("rating"),
        "pending": d.status == "pending",
        "raw": _fmt_pct(p.get("raw_return")),
        "alpha": _fmt_pct(p.get("alpha_return")),
        "holding": f"{p['holding_days']}d" if p.get("holding_days") else None,
        "decision": p.get("decision_text") or "",
        "reflection": p.get("reflection") or "",
    }


def _format_full(d: Decision) -> str:
    p = d.payload or {}
    raw = _fmt_pct(p.get("raw_return")) or "n/a"
    alpha = _fmt_pct(p.get("alpha_return")) or "n/a"
    holding = f"{p['holding_days']}d" if p.get("holding_days") else "n/a"
    rating = p.get("rating") or "?"
    tag = (
        f"[{d.trade_date.isoformat()} | {d.ticker} | {rating} "
        f"| {raw} | {alpha} | {holding}]"
    )
    parts = [tag, f"DECISION:\n{p.get('decision_text', '')}"]
    if p.get("reflection"):
        parts.append(f"REFLECTION:\n{p['reflection']}")
    return "\n\n".join(parts)


def _format_reflection_only(d: Decision) -> str:
    p = d.payload or {}
    raw = _fmt_pct(p.get("raw_return")) or "n/a"
    rating = p.get("rating") or "?"
    tag = f"[{d.trade_date.isoformat()} | {d.ticker} | {rating} | {raw}]"
    if p.get("reflection"):
        return f"{tag}\n{p['reflection']}"
    text = (p.get("decision_text") or "")[:300]
    suffix = "..." if len(p.get("decision_text", "") or "") > 300 else ""
    return f"{tag}\n{text}{suffix}"
