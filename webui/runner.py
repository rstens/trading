"""Background runner for web-UI analysis jobs.

Multiple concurrent jobs are supported — one daemon thread per job, with
no cross-job locking. The dataflows config mirror is a ContextVar (see
`tradingagents.dataflows.config`), so each job's thread sees its own
provider / vendor / output-language settings without racing siblings.

Jobs sit in the registry indefinitely after completion so the browser
can poll their status, render their tabs, and download the final JSON.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

from cli.stats_handler import StatsCallbackHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from webui.models import PROVIDER_BASE_URL, RunSelections


log = logging.getLogger("tradingagents.webui.runner")


def _now() -> datetime:
    """Aware UTC now. All job timestamps use this so the cache-hydration
    path (also tz-aware UTC) and live runs never mix naive/aware values —
    which would crash sorting and any timestamp comparison."""
    return datetime.now(timezone.utc)


def _epoch(dt: Optional[datetime]) -> float:
    """Epoch seconds for sorting; tz-safe across naive/aware inputs.
    Naive values are assumed local. Missing timestamps sort last."""
    if dt is None:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret naive as local time
    return dt.timestamp()


class JobCancelled(Exception):
    """Raised by _ProgressCallback when the user requested cancellation.

    Propagates out of LangGraph's pregel runner so `propagate()` returns
    via the `_run()` exception handler, which marks the job as
    `cancelled` (a distinct terminal state from `error`).
    """


# Map LangGraph node name → friendly agent label. Kept in sync with the
# nodes registered in `tradingagents.graph.setup.GraphSetup.setup_graph`.
NODE_LABELS = {
    "Market Analyst":        "Market Analyst",
    "Social Analyst":        "Sentiment Analyst",
    "News Analyst":          "News Analyst",
    "Fundamentals Analyst":  "Fundamentals Analyst",
    "Competition Analyst":   "Competition Analyst",
    "Bull Researcher":       "Bull Researcher",
    "Bear Researcher":       "Bear Researcher",
    "Research Manager":      "Research Manager",
    "Trader":                "Trader",
    "Aggressive Analyst":    "Aggressive Analyst",
    "Conservative Analyst":  "Conservative Analyst",
    "Neutral Analyst":       "Neutral Analyst",
    "Portfolio Manager":     "Portfolio Manager",
    "Hedging Agent":         "Hedging Agent",
}


# (key, label, source-field-on-state). The web UI shows one tab per agent.
# Source fields are *flat* keys on JobState.partial_state — the
# _ProgressCallback flattens nested debate state into top-level keys
# (bull_history, bear_history, aggressive_history, ...) so the template
# doesn't need a dotted-path resolver.
TAB_SECTIONS: List[tuple[str, str, str]] = [
    ("market",       "Market Analyst",        "market_report"),
    ("sentiment",    "Sentiment Analyst",     "sentiment_report"),
    ("news",         "News Analyst",          "news_report"),
    ("fundamentals", "Fundamentals Analyst",  "fundamentals_report"),
    ("competition",  "Competition Analyst",   "competition_report"),
    ("bull",         "Bull Researcher",       "bull_history"),
    ("bear",         "Bear Researcher",       "bear_history"),
    ("research",     "Research Manager",      "investment_plan"),
    ("trader",       "Trader",                "trader_investment_plan"),
    ("aggressive",   "Aggressive Analyst",    "aggressive_history"),
    ("conservative", "Conservative Analyst",  "conservative_history"),
    ("neutral",      "Neutral Analyst",       "neutral_history"),
    ("portfolio",    "Portfolio Manager",     "final_trade_decision"),
    ("hedging",      "Hedging Agent",         "hedging_report"),
]


# Top-level state fields that the _ProgressCallback should mirror into
# JobState.partial_state when a node emits them. Kept in sync with
# AgentState's typed fields.
_FLAT_SECTION_FIELDS = (
    "market_report", "sentiment_report", "news_report",
    "fundamentals_report", "competition_report",
    "investment_plan", "trader_investment_plan", "final_trade_decision",
    "hedging_report",
)

# Mapping from nested-state container → sub-keys to lift onto partial_state.
_NESTED_SECTION_FIELDS = {
    "investment_debate_state": ("bull_history", "bear_history"),
    "risk_debate_state": (
        "aggressive_history", "conservative_history", "neutral_history",
    ),
}


# Tabs produced AFTER the LangGraph pipeline finishes. Rendered alongside
# TAB_SECTIONS by the template, but excluded from the progress denominator
# (these don't correspond to graph nodes).
POST_PIPELINE_SECTIONS: List[tuple[str, str, str]] = [
    ("summary", "Summary", "summary"),
]

# Convenience for callers/templates that want every tab in display order.
ALL_TAB_SECTIONS: List[tuple[str, str, str]] = TAB_SECTIONS + POST_PIPELINE_SECTIONS


@dataclass
class JobState:
    id: str
    selections: RunSelections
    status: str = "queued"       # queued | running | done | error | cancelled
    current_agent: Optional[str] = None
    # Set by JobRegistry.cancel(); _ProgressCallback checks this on every
    # node/LLM/tool start and raises JobCancelled to unwind the pipeline.
    cancel_requested: bool = False
    # Best-effort full company name (e.g. "NVIDIA Corporation"), resolved
    # from yfinance at job start. Falls back to None when the lookup fails
    # or the ticker isn't recognized.
    company_name: Optional[str] = None
    completed_agents: List[str] = field(default_factory=list)
    # True when the result was loaded from disk cache instead of running
    # the pipeline. cached_at carries the file's mtime so the UI can
    # display "cached from N hours ago".
    from_cache: bool = False
    cached_at: Optional[datetime] = None
    # Postgres UUID linking this in-memory JobState to its persisted
    # `runs` row, when DB is configured. Used by the Recent jobs hybrid
    # merge to dedupe (in-memory wins) and by per-job persistence
    # transitions. Stays None when DB is unconfigured.
    db_run_id: Optional[Any] = None
    # Accumulated per-section content. Grows as each agent finishes; the
    # template renders tabs off this so results appear incrementally while
    # the run is still in flight.
    partial_state: Dict[str, str] = field(default_factory=dict)
    # The canonical, fully-merged final state. None until propagate()
    # returns successfully. Used by the JSON export endpoint.
    final_state: Optional[Dict[str, Any]] = None
    decision: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    stats: Dict[str, int] = field(default_factory=lambda: {
        "llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0,
    })

    @property
    def status_label(self) -> str:
        """Human-readable status for the UI.

        The internal `status` string stays as `"cancelled"` (matches
        `JobCancelled`, `cancel_requested`, and every code path that
        branches on it), but the user-facing badge reads ``Stop`` —
        same verb as the button that produced the state.
        """
        if self.status == "cancelled":
            return "Stop"
        return self.status

    def section_content(self, source_field: str) -> Optional[str]:
        """Return the markdown for a tab section, or None if not ready yet."""
        value = self.partial_state.get(source_field)
        if not value:
            return None
        return value if isinstance(value, str) else str(value)

    def sections_done(self) -> int:
        """Count of agents whose result has populated a tab.

        Reads from partial_state rather than the raw on_chain_end count so
        the number matches what the user actually sees: analyst tool-loop
        iterations don't inflate the count, and an agent only ticks over
        once its report is truly non-empty.
        """
        return sum(
            1 for _key, _label, src in TAB_SECTIONS
            if self.partial_state.get(src)
        )

    def progress_pct(self, total_agents: int) -> int:
        if self.status == "done":
            return 100
        if total_agents <= 0:
            return 0
        return min(100, int(100 * self.sections_done() / total_agents))


class _ProgressCallback(BaseCallbackHandler):
    """LangChain callback that updates JobState as graph nodes start/end.

    LangGraph fires per-node callbacks like:
      on_chain_start(serialized=None, input, name=<node_name>, run_id=<uuid>)
      on_chain_end(output)   # name is NOT re-dispatched by the callback
                             # manager; only run_id ties end → start.
    So we capture name→run_id in start, look it up in end. The map only
    holds entries for nodes whose name matches one of our agents — keeps
    it small even though LangGraph fires on_chain_start for many internal
    Runnables we don't care about (tool nodes, msg-clear nodes, sequence
    sub-runs, etc.).
    """

    def __init__(self, job: JobState, registry: "JobRegistry") -> None:
        super().__init__()
        self.job = job
        self.registry = registry
        # run_id (UUID) → registered node name. Populated in on_chain_start
        # only when the name matches NODE_LABELS, popped in on_chain_end.
        self._run_names: Dict[Any, str] = {}

    def _check_cancelled(self) -> None:
        """Raise JobCancelled if the user clicked Stop.

        Called on every start-side callback so cancellation propagates as
        soon as the next graph node / LLM call / tool invocation begins.
        Doesn't touch the registry lock — `cancel_requested` is a single
        bool, dict access is GIL-atomic, no torn read possible.
        """
        if self.job.cancel_requested:
            raise JobCancelled("Analysis cancelled by user")

    def on_chain_start(
        self,
        serialized: Optional[Dict[str, Any]] = None,
        inputs: Any = None,
        **kwargs: Any,
    ) -> None:
        self._check_cancelled()
        name = kwargs.get("name") or ""
        if name not in NODE_LABELS:
            return
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._run_names[run_id] = name
        with self.registry.lock:
            self.job.current_agent = NODE_LABELS[name]

    def on_chain_end(self, outputs: Any = None, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        name = self._run_names.pop(run_id, None) if run_id is not None else None
        if not name:
            return  # not one of our tracked agent nodes
        with self.registry.lock:
            label = NODE_LABELS[name]
            if label not in self.job.completed_agents:
                self.job.completed_agents.append(label)
            self._absorb_into_partial(outputs)

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        # Don't let cancelled / errored runs leak their entries.
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._run_names.pop(run_id, None)

    # Cancellation also checks on LLM and tool starts so the unwind
    # happens as soon as the next API call / tool dispatch begins, not
    # just on node-boundary transitions.
    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self._check_cancelled()

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self._check_cancelled()

    def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        self._check_cancelled()

    def _absorb_into_partial(self, outputs: Any) -> None:
        """Lift section content out of a node's state-diff into partial_state.

        Each node returns a partial AgentState dict; we copy the
        sections-of-interest into the registry's flat partial_state so the
        polling endpoint can render their tabs the moment they're ready.
        Empty strings (returned by analyst nodes during their tool-loop
        iterations before the final report is written) are skipped so a
        partially-populated tab never flashes empty.
        """
        if not isinstance(outputs, dict):
            return
        for key in _FLAT_SECTION_FIELDS:
            value = outputs.get(key)
            if value and isinstance(value, str):
                self.job.partial_state[key] = value
        for container, sub_keys in _NESTED_SECTION_FIELDS.items():
            nested = outputs.get(container)
            if not isinstance(nested, dict):
                continue
            for key in sub_keys:
                value = nested.get(key)
                if value and isinstance(value, str):
                    self.job.partial_state[key] = value


class JobRegistry:
    """Thread-safe registry of analysis jobs. Multiple jobs run concurrently."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._jobs: Dict[str, JobState] = {}

    def get(self, job_id: str) -> Optional[JobState]:
        with self.lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[JobState]:
        with self.lock:
            # Sort on epoch seconds, not datetimes: jobs hydrated from the
            # disk cache carry tz-aware timestamps while live runs may have
            # naive ones, and comparing the two raises TypeError. Reducing
            # to floats sidesteps the awareness mismatch entirely.
            return sorted(
                self._jobs.values(),
                key=lambda j: _epoch(j.started_at),
                reverse=True,
            )

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for j in self._jobs.values() if j.status == "running")

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation of a running job.

        Returns True if the job was running and the flag was set, False
        if the job is unknown or already terminal. Actual unwind happens
        inside _ProgressCallback on the next callback event; the user
        sees the status flip to ``cancelled`` within ~one LLM call.
        """
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in ("queued", "running"):
                return False
            job.cancel_requested = True
        return True

    def start(self, selections: RunSelections) -> JobState:
        """Register a job, return immediately if a recent cache exists,
        otherwise spin up the background thread.

        Cache check: looks for a webui_cache file for `(ticker, date)`
        whose mtime is within 7 days. Hit → populate JobState from disk
        and return as `status="done"` without touching the LangGraph
        pipeline. Skipped entirely when `selections.force_refresh` is set.

        No cross-job serialization — multiple users' fresh runs go in
        parallel daemon threads, each with its own ContextVar-scoped
        config (see tradingagents.dataflows.config).
        """
        from webui.cache import find_cache_hit, load_cache  # local import
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.persistence.runs import find_cache_hit_db

        with self.lock:
            job = JobState(id=uuid.uuid4().hex[:12], selections=selections)
            self._jobs[job.id] = job

        if not selections.force_refresh:
            # 1. DB cache (canonical when DATABASE_URL is set). Returns
            #    None on no-DB / no-hit so the file fallback can run.
            db_hit = find_cache_hit_db(
                selections.ticker, selections.analysis_date,
            )
            if db_hit is not None:
                self._populate_from_cache(
                    job,
                    {
                        "company_name": db_hit.get("company_name"),
                        "final_state": db_hit.get("final_state"),
                        "partial_state": db_hit.get("partial_state") or {},
                        "decision": db_hit.get("decision"),
                    },
                    db_hit["finished_at"].timestamp(),
                )
                # Point at the source DB row so Recent jobs dedupes us
                # against it instead of double-listing.
                job.db_run_id = db_hit["run_id"]
                log.info(
                    "[%s] DB cache hit: %s on %s (source run=%s)",
                    job.id, selections.ticker, selections.analysis_date,
                    db_hit["run_id"],
                )
                return job

            # 2. File cache (degraded fallback — useful when DB is off,
            #    or when the user has legacy file caches from before
            #    persistence was wired up).
            cache_path = find_cache_hit(
                DEFAULT_CONFIG["results_dir"],
                selections.ticker,
                selections.analysis_date,
            )
            if cache_path is not None:
                try:
                    payload = load_cache(cache_path)
                    self._populate_from_cache(job, payload, cache_path.stat().st_mtime)
                    log.info("[%s] file cache hit: %s on %s (%s)",
                             job.id, selections.ticker, selections.analysis_date, cache_path)
                    return job
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] cache load failed (%s) — falling through to fresh run",
                                job.id, e)

        thread = threading.Thread(target=self._run, args=(job.id,), daemon=True)
        thread.start()
        return job

    def _persist_terminal_no_cache(self, job: JobState, *, status: str) -> None:
        """Update the persisted run for non-success terminals.

        Errored and cancelled runs are NOT cached for reuse — they're
        marked terminal so the Recent jobs table shows them. Partial
        reports (whatever the callback absorbed before the failure) ARE
        persisted so the user can still see what got produced. No-op
        when DB is unconfigured or the row doesn't exist.
        """
        if job.db_run_id is None:
            return
        from tradingagents.persistence.runs import persist_run_completion
        from webui.cache import json_safe

        persist_run_completion(
            job.db_run_id,
            status=status,
            decision=job.decision,
            error=job.error,
            traceback=job.traceback,
            raw_state=json_safe(job.final_state) if job.final_state else None,
            partial_state=dict(job.partial_state),
            usage=job.stats,
            company_name=job.company_name,
        )

    def _populate_from_cache(self, job: JobState, payload: Dict[str, Any], mtime: float) -> None:
        """Fill in JobState from a cache payload so the UI renders the
        tabs immediately on the first poll."""
        # UTC-aware so the `local_dt` Jinja filter renders correctly
        # regardless of host TZ (Docker = UTC, dev host may not be).
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
        with self.lock:
            job.from_cache = True
            job.cached_at = ts
            job.company_name = payload.get("company_name")
            job.final_state = payload.get("final_state")
            job.partial_state = dict(payload.get("partial_state") or {})
            job.decision = payload.get("decision")
            job.status = "done"
            job.started_at = ts
            job.finished_at = ts

    def _run(self, job_id: str) -> None:
        # Local imports — only Phase 2+ persistence call sites depend on
        # the persistence package, and `webui.cache` is the legacy
        # file-based fallback used when DB is off.
        from tradingagents.persistence.runs import (
            insert_run,
            persist_run_completion,
            update_company_name,
        )

        job = self._jobs[job_id]
        try:
            job.status = "running"
            job.started_at = _now()

            # DB row at start — captures the run even if the worker
            # crashes mid-pipeline. No-op when DB is unconfigured.
            job.db_run_id = insert_run(
                ticker=job.selections.ticker,
                analysis_date=job.selections.analysis_date,
                selections=job.selections.model_dump(),
            )

            # Best-effort company-name resolution. Done in the worker thread
            # so the (network-bound) lookup doesn't block /api/jobs from
            # returning a redirect promptly. Failure is silent — the
            # results page just shows the bare ticker.
            try:
                import yfinance as _yf  # local import: keeps the module's
                # top-level imports fast for tests that don't touch network.
                info = _yf.Ticker(job.selections.ticker).info or {}
                job.company_name = info.get("longName") or info.get("shortName") or None
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] company-name lookup failed: %s", job.id, e)
            # Patch the resolved name onto the DB row so cache hits and
            # Recent jobs show the full name without a yfinance round-trip.
            if job.db_run_id is not None and job.company_name:
                update_company_name(job.db_run_id, job.company_name)

            log.info("[%s] starting %s on %s", job.id, job.selections.ticker, job.selections.analysis_date)

            config = _build_config(job.selections)
            stats = StatsCallbackHandler()
            graph = TradingAgentsGraph(
                selected_analysts=job.selections.analysts,
                config=config,
                debug=False,
                callbacks=[stats, _ProgressCallback(job, self)],
            )
            # Pass the resolved company name through to the graph so its
            # `get_past_context` call can build a more descriptive
            # vector-retrieval seed. None is fine — seed falls back to
            # ticker-only.
            graph.company_name = job.company_name
            final_state, decision = graph.propagate(
                job.selections.ticker, job.selections.analysis_date
            )

            with self.lock:
                job.final_state = final_state
                # Defensive: backfill anything the per-node callback missed.
                # Canonical final state is authoritative; callbacks are a
                # convenience for live updates during the run.
                for key in _FLAT_SECTION_FIELDS:
                    value = final_state.get(key)
                    if value and isinstance(value, str):
                        job.partial_state[key] = value
                for container, sub_keys in _NESTED_SECTION_FIELDS.items():
                    nested = final_state.get(container)
                    if isinstance(nested, dict):
                        for key in sub_keys:
                            value = nested.get(key)
                            if value and isinstance(value, str):
                                job.partial_state[key] = value
                job.decision = str(decision) if decision else None
                # Job stays in `running` so the user sees the summary phase
                # as an active step rather than the run going dark.
                job.current_agent = "Generating summary…"
            log.info("[%s] pipeline done: %s — generating summary", job.id, decision)

            # Post-pipeline summary. Non-fatal: if the LLM call errors
            # (rate limit, network blip, model rejected the prompt), log
            # and continue. The rest of the run is still fully usable.
            try:
                from webui.summary import generate_summary  # local import
                summary_md = generate_summary(config, final_state)
                with self.lock:
                    job.partial_state["summary"] = summary_md
                log.info("[%s] summary ready (%d chars)", job.id, len(summary_md))
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] summary generation failed: %s", job.id, e)

            with self.lock:
                # stats includes the summary LLM call too.
                job.stats = stats.get_stats()
                job.status = "done"
                job.finished_at = _now()
                job.current_agent = None
            log.info("[%s] done", job.id)

            # Persist completion. When DB is on, this is the canonical
            # write (runs row + per-section reports + token usage in one
            # transaction). When DB is off, fall back to the legacy
            # JSON file cache under ~/.tradingagents/logs/.
            persisted = False
            if job.db_run_id is not None:
                from webui.cache import json_safe
                persisted = persist_run_completion(
                    job.db_run_id,
                    status="done",
                    decision=job.decision,
                    error=None,
                    traceback=None,
                    raw_state=json_safe(job.final_state),
                    partial_state=dict(job.partial_state),
                    usage=job.stats,
                    company_name=job.company_name,
                )

            # Vector-store insertion (Phase 4). Only runs when the
            # canonical DB write succeeded — embeddings without a
            # parent `runs` row would orphan, and the foreign-key
            # cascade keeps the vector store consistent if the run
            # later gets deleted. Non-fatal: a vector-store outage
            # degrades RAG context but doesn't fail the analysis.
            if persisted and job.db_run_id is not None:
                try:
                    from tradingagents.persistence.embeddings import (
                        DEFAULT_EMBED_SECTIONS,
                        persist_embeddings_for_run,
                    )
                    sections_to_embed = {
                        key: job.partial_state.get(key)
                        for key in DEFAULT_EMBED_SECTIONS
                        if job.partial_state.get(key)
                    }
                    if sections_to_embed:
                        n = persist_embeddings_for_run(
                            job.db_run_id,
                            job.selections.ticker,
                            sections_to_embed,
                        )
                        log.info("[%s] vector store: %d embeddings persisted",
                                 job.id, n)
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] embedding persistence failed: %s",
                                job.id, e)
            if not persisted:
                try:
                    from webui.cache import save_cache, json_safe
                    save_cache(
                        config["results_dir"],
                        job.selections.ticker,
                        job.selections.analysis_date,
                        {
                            "ticker": job.selections.ticker,
                            "analysis_date": job.selections.analysis_date,
                            "company_name": job.company_name,
                            "decision": job.decision,
                            "selections": job.selections.model_dump(),
                            "completed_at": job.finished_at.isoformat() if job.finished_at else None,
                            "final_state": json_safe(job.final_state),
                            "partial_state": dict(job.partial_state),
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] file cache save failed: %s", job.id, e)
        except JobCancelled:
            log.info("[%s] cancelled by user", job_id)
            with self.lock:
                job.status = "cancelled"
                job.error = "Analysis cancelled by user"
                job.finished_at = _now()
                job.current_agent = None
            self._persist_terminal_no_cache(job, status="cancelled")
        except Exception as e:
            # If the user cancelled and the underlying SDK wrapped our
            # JobCancelled inside a retry/transport exception, still treat
            # it as a cancellation rather than a hard error.
            if job.cancel_requested:
                log.info("[%s] cancelled by user (raised as %s)", job_id, type(e).__name__)
                with self.lock:
                    job.status = "cancelled"
                    job.error = "Analysis cancelled by user"
                    job.finished_at = _now()
                    job.current_agent = None
                self._persist_terminal_no_cache(job, status="cancelled")
            else:
                log.exception("[%s] failed", job_id)
                with self.lock:
                    job.status = "error"
                    job.error = str(e)
                    job.traceback = traceback.format_exc()
                    job.finished_at = _now()
                self._persist_terminal_no_cache(job, status="error")


def _build_config(selections: RunSelections) -> Dict[str, Any]:
    """Translate form selections into a TradingAgentsGraph config dict.

    Layered on top of DEFAULT_CONFIG so any `TRADINGAGENTS_*` env-var
    overrides the user already has are preserved unless the form
    explicitly overrides them.
    """
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = selections.llm_provider
    config["quick_think_llm"] = selections.quick_thinker
    config["deep_think_llm"] = selections.deep_thinker
    config["backend_url"] = PROVIDER_BASE_URL.get(selections.llm_provider)
    config["max_debate_rounds"] = selections.research_depth
    config["max_risk_discuss_rounds"] = selections.research_depth
    config["output_language"] = selections.output_language
    config["openai_reasoning_effort"] = selections.openai_reasoning_effort
    config["anthropic_effort"] = selections.anthropic_effort
    config["google_thinking_level"] = selections.google_thinking_level
    return config


# Module-global registry. FastAPI dependency-injects this via Depends().
_registry: Optional[JobRegistry] = None


def get_registry() -> JobRegistry:
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry
