"""Re-embed every `done` run with the currently-configured embedding provider.

Use this when:
  * switching `TRADINGAGENTS_EMBEDDING_PROVIDER` (e.g. openai → google),
  * switching `TRADINGAGENTS_EMBEDDING_MODEL` (e.g. text-embedding-3-small
    → text-embedding-3-large), or
  * after migrating the `run_embeddings.embedding` column to a different
    `vector(N)` width.

The script wipes existing `run_embeddings` rows, then walks every `done`
run, fetches its `summary` / `final_trade_decision` / `investment_plan`
sections from `run_reports`, and re-embeds them under the new provider.

Resilient to per-run API failures — they're logged and skipped so a
flaky cell doesn't stop the rest of the backfill.

Usage::

    python scripts/reembed.py
    python scripts/reembed.py --keep-existing      # don't wipe first

Requires `TRADINGAGENTS_DATABASE_URL` and the provider's API key.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict

from sqlalchemy import delete, select

from tradingagents.persistence import session_scope
from tradingagents.persistence.embeddings import (
    DEFAULT_EMBED_SECTIONS,
    EmbeddingService,
    persist_embeddings_for_run,
)
from tradingagents.persistence.models import Run, RunEmbedding, RunReport


def _wipe_existing() -> int:
    with session_scope() as s:
        if s is None:
            raise SystemExit("TRADINGAGENTS_DATABASE_URL not set.")
        result = s.execute(delete(RunEmbedding))
        return result.rowcount or 0


def _fetch_run_ids_and_sections() -> Dict:
    """Snapshot of (run_id, ticker) → {section: content} for all done runs.

    We materialize this in one query / one session, then release the session
    before re-embedding. Each per-run embed below opens its own short-lived
    session so a long re-embed run doesn't pin a single connection.
    """
    rows: Dict = {}
    with session_scope() as s:
        if s is None:
            raise SystemExit("TRADINGAGENTS_DATABASE_URL not set.")
        # Pull the runs + reports we care about. Single query; the WHERE
        # clause filters to the sections we actually embed.
        stmt = (
            select(Run.id, Run.ticker, RunReport.section, RunReport.content)
            .join(RunReport, RunReport.run_id == Run.id)
            .where(Run.status == "done")
            .where(RunReport.section.in_(DEFAULT_EMBED_SECTIONS))
            .order_by(Run.started_at)
        )
        for run_id, ticker, section, content in s.execute(stmt).all():
            key = (run_id, ticker)
            rows.setdefault(key, {})[section] = content
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-existing", action="store_true",
        help="Do not delete current embeddings before re-embedding.",
    )
    args = parser.parse_args(argv)

    service = EmbeddingService()
    print(
        f"Embedding provider={service.provider} model={service.model} "
        f"dim={service.expected_dim}",
    )

    if not args.keep_existing:
        deleted = _wipe_existing()
        print(f"Cleared {deleted} existing embedding rows.")

    work = _fetch_run_ids_and_sections()
    print(f"Re-embedding {len(work)} done runs...")

    inserted_total = 0
    failed = 0
    for i, ((run_id, ticker), sections) in enumerate(work.items(), 1):
        try:
            n = persist_embeddings_for_run(run_id, ticker, sections, service=service)
            inserted_total += n
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(work)}] {ticker} ({run_id}): FAILED — {e}")
            continue
        if i % 10 == 0 or i == len(work):
            print(f"  [{i}/{len(work)}] {ticker}: +{n} rows (total inserted: {inserted_total})")

    print(
        f"Re-embed complete: {inserted_total} rows inserted across "
        f"{len(work) - failed}/{len(work)} runs, {failed} runs failed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
