"""One-shot backfill of the legacy markdown decision log into Postgres.

Reads `~/.tradingagents/memory/trading_memory.md` (or a path arg) and
inserts pending / resolved rows into the `decisions` table. Idempotent
via the `UNIQUE(ticker, trade_date)` constraint — running twice is
safe (the second run reports them all as `skipped`).

Usage::

    python scripts/backfill_decisions.py
    python scripts/backfill_decisions.py /path/to/trading_memory.md

Requires `TRADINGAGENTS_DATABASE_URL` to be set. Schema is assumed to
be already applied (`python -m tradingagents.persistence upgrade`).
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

from sqlalchemy import select

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence import Decision, session_scope


_PCT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")


def _parse_percent(value: object) -> float | None:
    """Convert a "%-formatted" decision-log value to a 0–1.0 float.

    The markdown log stores returns like "+1.3%" or "-0.7%" or "n/a";
    we want them back as numeric so JSONB stays queryable.
    """
    if not isinstance(value, str):
        return None
    m = _PCT_RE.match(value.strip())
    if not m:
        return None
    try:
        return float(m.group(1)) / 100.0
    except ValueError:
        return None


def _parse_holding(value: object) -> int | None:
    """Convert "5d" → 5."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    try:
        return int(s)
    except ValueError:
        return None


def backfill(md_path: Path) -> tuple[int, int, int]:
    """Run the import. Returns (inserted, skipped, malformed) counts."""
    # TradingMemoryLog._parse_entry already knows the markdown format,
    # so we cheat and reuse it instead of re-implementing the parser.
    # Pass an explicit path; the class accepts None gracefully.
    log = TradingMemoryLog({"memory_log_path": str(md_path)})
    # `load_entries` walks the file directly — DB on/off doesn't matter
    # here because there's no get_*-from-DB call.
    entries = log.load_entries()

    inserted = skipped = malformed = 0
    if not entries:
        return (0, 0, 0)

    with session_scope() as s:
        if s is None:
            raise SystemExit(
                "TRADINGAGENTS_DATABASE_URL is not set — cannot backfill."
            )

        for entry in entries:
            try:
                trade_date = datetime.date.fromisoformat(entry["date"])
                ticker = entry["ticker"]
            except (KeyError, ValueError, TypeError):
                malformed += 1
                continue

            existing = s.execute(
                select(Decision)
                .where(Decision.ticker == ticker)
                .where(Decision.trade_date == trade_date)
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue

            payload = {
                "rating": entry.get("rating"),
                "decision_text": entry.get("decision", ""),
            }
            if entry.get("reflection"):
                payload["reflection"] = entry["reflection"]

            raw_pct = _parse_percent(entry.get("raw"))
            alpha_pct = _parse_percent(entry.get("alpha"))
            holding = _parse_holding(entry.get("holding"))
            if raw_pct is not None:
                payload["raw_return"] = raw_pct
            if alpha_pct is not None:
                payload["alpha_return"] = alpha_pct
            if holding is not None:
                payload["holding_days"] = holding

            status = "pending" if entry.get("pending") else "resolved"
            row = Decision(
                ticker=ticker,
                trade_date=trade_date,
                status=status,
                payload=payload,
            )
            if status == "resolved":
                row.resolved_at = datetime.datetime.now(datetime.timezone.utc)
            s.add(row)
            inserted += 1

    return (inserted, skipped, malformed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?",
        help="Path to trading_memory.md; defaults to config.memory_log_path.",
    )
    args = parser.parse_args(argv)

    md_path_str = args.path or DEFAULT_CONFIG.get("memory_log_path")
    if not md_path_str:
        print("No memory_log_path configured and no path argument given.", file=sys.stderr)
        return 2

    md_path = Path(md_path_str).expanduser()
    if not md_path.exists():
        print(f"Markdown decision log not found: {md_path} (nothing to backfill).")
        return 0

    inserted, skipped, malformed = backfill(md_path)
    print(
        f"Backfill complete: {inserted} inserted, {skipped} skipped (already "
        f"present), {malformed} malformed. Source: {md_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
