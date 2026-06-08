"""HTTP routes for the web UI.

Split into three groups:
  * Page routes (HTML responses): the index, plus the per-job detail page.
  * HTMX fragment routes: provider→models swap, effort selector, polled
    job status fragment.
  * API routes: start job, export final state as JSON.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from cli.models import AnalystType
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

from webui.assessment import assess_job
from webui.batch import BatchRegistry, get_batch_registry
from webui.cache import json_safe
from webui.models import PROVIDERS, RunSelections
from webui.preferences import load_last_settings, save_last_settings
from webui.schedules import (
    SCHEDULE_TICKER_SENTINEL,
    ScheduleRegistry,
    get_schedule_registry,
)
from webui.runner import (
    ALL_TAB_SECTIONS,
    JobRegistry,
    NODE_LABELS,
    TAB_SECTIONS,
    get_registry,
)


router = APIRouter()


_FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.svg"


@router.get("/favicon.ico", include_in_schema=False)
def favicon_legacy() -> Response:
    """Serve the SVG favicon at the legacy /favicon.ico path.

    Modern browsers honor the `<link rel="icon" type="image/svg+xml">` in
    base.html, but bots, link previewers, and older clients still poke
    at /favicon.ico by default — without this route those requests log
    404s for every page load.
    """
    return FileResponse(_FAVICON_PATH, media_type="image/svg+xml")


@router.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Liveness probe for the Docker healthcheck.

    Returns 200 once the app's routes are registered. Deliberately does
    no work (no DB, no registry access) so it stays cheap and can't be
    made unhealthy by a degraded dependency — it answers "is the web
    process serving?", not "is everything downstream up?". Its access-log
    line is suppressed by the filter in `webui.app` so the 10 s poll
    doesn't flood the log.
    """
    return JSONResponse({"status": "ok"})


def _templates(request: Request) -> "Jinja2Templates":  # noqa: F821
    return request.app.state.templates


# ---------- Page routes ----------

@router.get("/", response_class=HTMLResponse)
def index(request: Request, registry: JobRegistry = Depends(get_registry)) -> Response:
    """Main page: selection form on top, details placeholder below.

    Provider list is filtered to those with an API key set in the
    environment (Ollama is always shown — it needs no key). Form fields
    repopulate from the last submitted run when available; analysis date
    always resets to today.
    """
    saved = load_last_settings()
    available = _available_providers()
    no_keys_warning = not any(
        get_api_key_env(key) for _, key, _ in available
    ) and len(available) <= 1  # Ollama-only

    # Empty ticker on first run; saved value on subsequent runs.
    default_ticker = saved.get("ticker", "")

    # Default provider: saved value if still available, else first available,
    # else fall back to the global default.
    available_keys = {k for _, k, _ in available}
    default_provider = (
        saved.get("llm_provider")
        if saved.get("llm_provider") in available_keys
        else (available[0][1] if available else DEFAULT_CONFIG["llm_provider"])
    )

    defaults = {
        "ticker": default_ticker,
        "analysis_date": datetime.date.today().isoformat(),
        "llm_provider": default_provider,
        "research_depth": saved.get("research_depth", DEFAULT_CONFIG["max_debate_rounds"]),
        "output_language": saved.get("output_language", DEFAULT_CONFIG["output_language"]),
        # Empty list means "every analyst checked" (handled in the template).
        "analysts": saved.get("analysts", [a.value for a in AnalystType]),
    }
    selected_effort = (
        saved.get("openai_reasoning_effort")
        or saved.get("anthropic_effort")
        or saved.get("google_thinking_level")
        or ""
    )
    provider_models = _models_for_provider(default_provider)
    active, terminal_today, older = _partition_jobs(registry)
    return _templates(request).TemplateResponse(
        request,
        "index.html",
        {
            "providers": available,
            "analysts": [(a.value, a.name.title()) for a in AnalystType],
            "defaults": defaults,
            "quick_models": provider_models["quick"],
            "deep_models": provider_models["deep"],
            "selected_quick": saved.get("quick_thinker"),
            "selected_deep": saved.get("deep_thinker"),
            "effort_html": _effort_html(request, default_provider, selected_effort),
            "running_count": registry.running_count(),
            # Hybrid: in-memory live state + DB terminal history, deduped
            # by db_run_id and sorted by started_at desc. One partition
            # call funds both panels.
            "jobs": active + terminal_today[:_TERMINAL_TODAY_CAP],
            "history": older[:_HISTORY_CAP],
            # Same rows, grouped one-line-per-ticker for the expandable
            # History panel.
            "history_groups": _group_history(older[:_HISTORY_CAP]),
            "no_keys_warning": no_keys_warning,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: str, request: Request, registry: JobRegistry = Depends(get_registry)
) -> Response:
    """Job-detail page: status block (polled) + tabs container.

    Resolution order:
      1. In-memory JobRegistry by job id (live progress for in-flight +
         recently finished runs).
      2. In-memory JobRegistry by db_run_id — the "open" links key off
         db_run_id when present, so a live job's durable URL still finds
         its live JobState.
      3. DB lookup by UUID for runs that have rolled off the registry
         (after server restart, or just a long-ago run).
      4. 404.
    """
    job = (
        registry.get(job_id)
        or registry.get_by_db_run_id(job_id)
        or _load_job_from_db(job_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return _templates(request).TemplateResponse(
        request,
        "job.html",
        {
            "job": job,
            "tab_sections": ALL_TAB_SECTIONS,
            "assessments": assess_job(job.partial_state, ALL_TAB_SECTIONS),
        },
    )


# ---------- HTMX fragment routes ----------

@router.get("/htmx/models", response_class=HTMLResponse)
def htmx_models(request: Request, llm_provider: str) -> Response:
    """Re-render the quick/deep model selects when the provider changes.

    Query parameter is `llm_provider` (not `provider`) so it matches the
    `name` attribute on the form's provider <select>, which is what
    `hx-include="[name='llm_provider']"` sends.
    """
    models = _models_for_provider(llm_provider)
    return _templates(request).TemplateResponse(
        request,
        "partials/models.html",
        {
            "quick_models": models["quick"],
            "deep_models": models["deep"],
        },
    )


@router.get("/htmx/effort", response_class=HTMLResponse)
def htmx_effort(request: Request, llm_provider: str) -> Response:
    """Re-render the provider-specific effort/thinking knob."""
    return Response(_effort_html(request, llm_provider), media_type="text/html")


@router.get("/htmx/company-name", response_class=HTMLResponse)
def htmx_company_name(ticker: str = "") -> Response:
    """Resolve a ticker to its full company name via yfinance.

    Debounced from the index page's ticker input. Returns an empty body for
    blank input (so the display element is cleared between edits) and a
    parenthetical hint when yfinance has nothing for the symbol.
    """
    ticker = (ticker or "").strip()
    if not ticker:
        return Response("", media_type="text/html")
    name = _lookup_company_name(ticker)
    if name:
        return Response(
            f'<span class="company-name">{html.escape(name)}</span>',
            media_type="text/html",
        )
    return Response(
        '<span class="dim">(unknown ticker — analysis will still attempt this symbol)</span>',
        media_type="text/html",
    )


@router.get("/htmx/jobs/{job_id}/status", response_class=HTMLResponse)
def htmx_job_status(
    job_id: str, request: Request, registry: JobRegistry = Depends(get_registry)
) -> Response:
    """Polled fragment with progress bar + tabs once results are in."""
    job = (
        registry.get(job_id)
        or registry.get_by_db_run_id(job_id)
        or _load_job_from_db(job_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    total_agents = _total_agents_for(job.selections.analysts)
    return _templates(request).TemplateResponse(
        request,
        "partials/job_status.html",
        {
            "job": job,
            "tab_sections": ALL_TAB_SECTIONS,
            "assessments": assess_job(job.partial_state, ALL_TAB_SECTIONS),
            "total_agents": total_agents,
            "progress_pct": job.progress_pct(total_agents),
        },
    )


# ---------- API routes ----------

@router.post("/api/jobs", response_class=HTMLResponse)
def start_job(
    request: Request,
    registry: JobRegistry = Depends(get_registry),
    ticker: str = Form(...),
    analysis_date: str = Form(...),
    # Unchecked-checkboxes don't submit at all, so analysts may be missing entirely.
    # Default to None and fall back to "all four analyst keys" below.
    analysts: Optional[list[str]] = Form(None),
    llm_provider: str = Form(...),
    quick_thinker: str = Form(...),
    deep_thinker: str = Form(...),
    research_depth: int = Form(1),
    output_language: str = Form("English"),
    openai_reasoning_effort: Optional[str] = Form(None),
    anthropic_effort: Optional[str] = Form(None),
    google_thinking_level: Optional[str] = Form(None),
    force_refresh: bool = Form(False),
) -> Response:
    """Submit the selection form; spawn the background job; redirect to detail page."""
    try:
        selections = RunSelections(
            ticker=ticker.strip(),
            analysis_date=analysis_date,
            analysts=analysts or [a.value for a in AnalystType],
            llm_provider=llm_provider,
            quick_thinker=quick_thinker,
            deep_thinker=deep_thinker,
            research_depth=research_depth,
            output_language=output_language,
            openai_reasoning_effort=openai_reasoning_effort or None,
            anthropic_effort=anthropic_effort or None,
            google_thinking_level=google_thinking_level or None,
            force_refresh=bool(force_refresh),
        )
    except Exception as e:
        return _templates(request).TemplateResponse(
            request,
            "partials/error.html",
            {"message": f"Invalid selections: {e}"},
            status_code=400,
        )

    env_var = get_api_key_env(selections.llm_provider)
    if env_var and not os.environ.get(env_var):
        return _templates(request).TemplateResponse(
            request,
            "partials/error.html",
            {
                "message": (
                    f"{env_var} is not set. Add it to .env (or the process "
                    f"environment) and restart the server, then resubmit."
                ),
            },
            status_code=400,
        )

    try:
        job = registry.start(selections)
    except RuntimeError as e:
        return _templates(request).TemplateResponse(
            request,
            "partials/error.html",
            {"message": str(e)},
            status_code=409,
        )

    # Persist the just-submitted form so the next page load defaults to it.
    # Failure to save is non-fatal — the job still runs; we just log.
    try:
        save_last_settings(selections.model_dump())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("tradingagents.webui").warning("Could not save last settings: %s", e)

    # HTMX honors HX-Redirect to do a full page navigation from a partial response.
    return HTMLResponse("", headers={"HX-Redirect": f"/jobs/{job.id}"})


@router.post("/api/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(
    job_id: str,
    request: Request,
    registry: JobRegistry = Depends(get_registry),
) -> Response:
    """Cooperative cancel — sets the job's cancel flag.

    The next callback event inside the pipeline (typically within one
    in-flight LLM call) sees the flag and unwinds. We return the current
    status fragment immediately so HTMX swaps the Stop button out and
    the user gets visible feedback without waiting for the next poll.
    """
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    registry.cancel(job_id)
    # Re-render the status fragment so the Stop button disables right
    # away. The job may still show "running" until the unwind happens —
    # the normal 2 s poll will pick up the "cancelled" transition.
    total_agents = _total_agents_for(job.selections.analysts)
    return _templates(request).TemplateResponse(
        request,
        "partials/job_status.html",
        {
            "job": job,
            "tab_sections": ALL_TAB_SECTIONS,
            "assessments": assess_job(job.partial_state, ALL_TAB_SECTIONS),
            "total_agents": total_agents,
            "progress_pct": job.progress_pct(total_agents),
        },
    )


# ---------- Batch routes ----------

@router.get("/batch", response_class=HTMLResponse)
def batch_form(
    request: Request,
    job_reg: JobRegistry = Depends(get_registry),
    batch_reg: BatchRegistry = Depends(get_batch_registry),
) -> Response:
    """Batch submission page — same selection controls as the single-ticker
    form, but with a textarea for many tickers."""
    saved = load_last_settings()
    available = _available_providers()
    available_keys = {k for _, k, _ in available}
    default_provider = (
        saved.get("llm_provider")
        if saved.get("llm_provider") in available_keys
        else (available[0][1] if available else DEFAULT_CONFIG["llm_provider"])
    )
    defaults = {
        "analysis_date": datetime.date.today().isoformat(),
        "llm_provider": default_provider,
        "research_depth": saved.get("research_depth", DEFAULT_CONFIG["max_debate_rounds"]),
        "output_language": saved.get("output_language", DEFAULT_CONFIG["output_language"]),
        "analysts": saved.get("analysts", [a.value for a in AnalystType]),
    }
    selected_effort = (
        saved.get("openai_reasoning_effort")
        or saved.get("anthropic_effort")
        or saved.get("google_thinking_level")
        or ""
    )
    provider_models = _models_for_provider(default_provider)
    return _templates(request).TemplateResponse(
        request,
        "batch.html",
        {
            "providers": available,
            "analysts": [(a.value, a.name.title()) for a in AnalystType],
            "defaults": defaults,
            "quick_models": provider_models["quick"],
            "deep_models": provider_models["deep"],
            "selected_quick": saved.get("quick_thinker"),
            "selected_deep": saved.get("deep_thinker"),
            "effort_html": _effort_html(request, default_provider, selected_effort),
            # Hybrid: in-memory live state + DB terminal history,
            # deduped by db_batch_id (registry wins on collision).
            "batches": _merged_recent_batches(batch_reg, limit=20),
        },
    )


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(
    batch_id: str,
    request: Request,
    batch_reg: BatchRegistry = Depends(get_batch_registry),
) -> Response:
    batch = batch_reg.get(batch_id) or _load_batch_from_db(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Unknown batch id")
    return _templates(request).TemplateResponse(
        request, "batch_detail.html",
        {"batch": batch},
    )


@router.get("/htmx/batches/{batch_id}/status", response_class=HTMLResponse)
def htmx_batch_status(
    batch_id: str,
    request: Request,
    batch_reg: BatchRegistry = Depends(get_batch_registry),
) -> Response:
    batch = batch_reg.get(batch_id) or _load_batch_from_db(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Unknown batch id")
    return _templates(request).TemplateResponse(
        request, "partials/batch_status.html", {"batch": batch},
    )


@router.post("/api/batches", response_class=HTMLResponse)
def start_batch(
    request: Request,
    job_reg: JobRegistry = Depends(get_registry),
    batch_reg: BatchRegistry = Depends(get_batch_registry),
    tickers: str = Form(...),
    analysis_date: str = Form(...),
    analysts: Optional[list[str]] = Form(None),
    llm_provider: str = Form(...),
    quick_thinker: str = Form(...),
    deep_thinker: str = Form(...),
    research_depth: int = Form(1),
    output_language: str = Form("English"),
    openai_reasoning_effort: Optional[str] = Form(None),
    anthropic_effort: Optional[str] = Form(None),
    google_thinking_level: Optional[str] = Form(None),
    force_refresh: bool = Form(False),
) -> Response:
    """Submit a list of tickers as a batch. Tickers split on whitespace,
    commas, or semicolons — common formats users actually paste."""
    raw = (tickers or "").replace(",", "\n").replace(";", "\n")
    ticker_list = [line.strip() for line in raw.splitlines() if line.strip()]
    if not ticker_list:
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": "Provide at least one ticker."},
            status_code=400,
        )

    # Use a sentinel ticker just to satisfy the model — the dispatcher
    # overrides it with each real ticker via model_copy.
    try:
        base = RunSelections(
            ticker="__BATCH__",
            analysis_date=analysis_date,
            analysts=analysts or [a.value for a in AnalystType],
            llm_provider=llm_provider,
            quick_thinker=quick_thinker,
            deep_thinker=deep_thinker,
            research_depth=research_depth,
            output_language=output_language,
            openai_reasoning_effort=openai_reasoning_effort or None,
            anthropic_effort=anthropic_effort or None,
            google_thinking_level=google_thinking_level or None,
            force_refresh=bool(force_refresh),
        )
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": f"Invalid selections: {e}"}, status_code=400,
        )

    env_var = get_api_key_env(base.llm_provider)
    if env_var and not os.environ.get(env_var):
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": (
                f"{env_var} is not set. Add it to .env and restart the server, "
                "then resubmit."
            )},
            status_code=400,
        )

    try:
        batch = batch_reg.start(base, ticker_list)
    except ValueError as e:
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": str(e)}, status_code=400,
        )

    # Persist last selections (without the sentinel ticker) so the user's
    # provider / model / analysts pre-fill on the next visit.
    try:
        d = base.model_dump()
        d.pop("ticker", None)
        save_last_settings(d)
    except Exception:
        pass

    return HTMLResponse("", headers={"HX-Redirect": f"/batches/{batch.id}"})


@router.post("/api/batches/{batch_id}/cancel", response_class=HTMLResponse)
def cancel_batch(
    batch_id: str,
    request: Request,
    batch_reg: BatchRegistry = Depends(get_batch_registry),
) -> Response:
    batch = batch_reg.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Unknown batch id")
    batch_reg.cancel(batch_id)
    return _templates(request).TemplateResponse(
        request, "partials/batch_status.html", {"batch": batch},
    )


# ---------- end batch routes ----------


# ---------- Schedule routes ----------

@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
) -> Response:
    """Schedule management page — creation form + table of existing
    schedules. Same selection controls as the batch form, plus the
    cadence / time-of-day / weekday knobs."""
    saved = load_last_settings()
    available = _available_providers()
    available_keys = {k for _, k, _ in available}
    default_provider = (
        saved.get("llm_provider")
        if saved.get("llm_provider") in available_keys
        else (available[0][1] if available else DEFAULT_CONFIG["llm_provider"])
    )
    defaults = {
        "llm_provider": default_provider,
        "research_depth": saved.get("research_depth", DEFAULT_CONFIG["max_debate_rounds"]),
        "output_language": saved.get("output_language", DEFAULT_CONFIG["output_language"]),
        "analysts": saved.get("analysts", [a.value for a in AnalystType]),
    }
    selected_effort = (
        saved.get("openai_reasoning_effort")
        or saved.get("anthropic_effort")
        or saved.get("google_thinking_level")
        or ""
    )
    provider_models = _models_for_provider(default_provider)
    return _templates(request).TemplateResponse(
        request,
        "schedules.html",
        {
            "providers": available,
            "analysts": [(a.value, a.name.title()) for a in AnalystType],
            "defaults": defaults,
            "quick_models": provider_models["quick"],
            "deep_models": provider_models["deep"],
            "selected_quick": saved.get("quick_thinker"),
            "selected_deep": saved.get("deep_thinker"),
            "effort_html": _effort_html(request, default_provider, selected_effort),
            "schedules": sched_reg.list_schedules(),
        },
    )


@router.get("/htmx/schedules/table", response_class=HTMLResponse)
def htmx_schedules_table(
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
) -> Response:
    """Polled fragment so Next/Last-run columns stay fresh."""
    return _schedules_table(request, sched_reg)


@router.post("/api/schedules", response_class=HTMLResponse)
def create_schedule(
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
    name: str = Form(""),
    tickers: str = Form(...),
    cadence: str = Form("daily"),
    time_of_day: str = Form("07:00"),
    weekday: int = Form(0),
    analysts: Optional[list[str]] = Form(None),
    llm_provider: str = Form(...),
    quick_thinker: str = Form(...),
    deep_thinker: str = Form(...),
    research_depth: int = Form(1),
    output_language: str = Form("English"),
    openai_reasoning_effort: Optional[str] = Form(None),
    anthropic_effort: Optional[str] = Form(None),
    google_thinking_level: Optional[str] = Form(None),
    force_refresh: bool = Form(False),
) -> Response:
    """Create a recurring schedule. Ticker parsing matches /api/batches."""
    raw = (tickers or "").replace(",", "\n").replace(";", "\n")
    ticker_list = [line.strip() for line in raw.splitlines() if line.strip()]
    if not ticker_list:
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": "Provide at least one ticker."},
            status_code=400,
        )

    try:
        base = RunSelections(
            ticker=SCHEDULE_TICKER_SENTINEL,
            # Placeholder — overridden with the fire date each run.
            analysis_date=datetime.date.today().isoformat(),
            analysts=analysts or [a.value for a in AnalystType],
            llm_provider=llm_provider,
            quick_thinker=quick_thinker,
            deep_thinker=deep_thinker,
            research_depth=research_depth,
            output_language=output_language,
            openai_reasoning_effort=openai_reasoning_effort or None,
            anthropic_effort=anthropic_effort or None,
            google_thinking_level=google_thinking_level or None,
            force_refresh=bool(force_refresh),
        )
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": f"Invalid selections: {e}"}, status_code=400,
        )

    env_var = get_api_key_env(base.llm_provider)
    if env_var and not os.environ.get(env_var):
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": (
                f"{env_var} is not set. Add it to .env and restart the server, "
                "then resubmit."
            )},
            status_code=400,
        )

    try:
        sched_reg.create(
            name=name,
            tickers=ticker_list,
            cadence=cadence,
            time_of_day=time_of_day,
            weekday=weekday,
            base_selections=base,
        )
    except Exception as e:  # noqa: BLE001 — pydantic validation errors land here
        return _templates(request).TemplateResponse(
            request, "partials/error.html",
            {"message": f"Invalid schedule: {e}"}, status_code=400,
        )

    # Persist last selections (minus the sentinel ticker) like the
    # batch form does, so the next form visit pre-fills.
    try:
        d = base.model_dump()
        d.pop("ticker", None)
        save_last_settings(d)
    except Exception:  # noqa: BLE001
        pass

    return HTMLResponse("", headers={"HX-Redirect": "/schedules"})


@router.post("/api/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
def toggle_schedule(
    schedule_id: str,
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
) -> Response:
    schedule = sched_reg.get(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Unknown schedule id")
    sched_reg.set_enabled(schedule_id, not schedule.enabled)
    return _schedules_table(request, sched_reg)


@router.post("/api/schedules/{schedule_id}/delete", response_class=HTMLResponse)
def delete_schedule(
    schedule_id: str,
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
) -> Response:
    if not sched_reg.delete(schedule_id):
        raise HTTPException(status_code=404, detail="Unknown schedule id")
    return _schedules_table(request, sched_reg)


@router.post("/api/schedules/{schedule_id}/run", response_class=HTMLResponse)
def run_schedule_now(
    schedule_id: str,
    request: Request,
    sched_reg: ScheduleRegistry = Depends(get_schedule_registry),
) -> Response:
    """Fire immediately (extra run, cadence unchanged). On success,
    redirect to the new batch's detail page so the user can watch it."""
    if sched_reg.get(schedule_id) is None:
        raise HTTPException(status_code=404, detail="Unknown schedule id")
    batch_id = sched_reg.run_now(schedule_id)
    if batch_id is not None:
        return HTMLResponse("", headers={"HX-Redirect": f"/batches/{batch_id}"})
    # Failure (missing key, bad stored selections, ...) — re-render the
    # table; the schedule's last_error column carries the reason.
    return _schedules_table(request, sched_reg)


def _schedules_table(request: Request, sched_reg: ScheduleRegistry) -> Response:
    return _templates(request).TemplateResponse(
        request,
        "partials/schedules_table.html",
        {"schedules": sched_reg.list_schedules()},
    )


# ---------- end schedule routes ----------


@router.get("/api/jobs/{job_id}/export.json")
def export_job_json(
    job_id: str, registry: JobRegistry = Depends(get_registry)
) -> Response:
    """Download the full final state of a completed job as a JSON file."""
    job = registry.get(job_id) or _load_job_from_db(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if job.final_state is None:
        raise HTTPException(status_code=409, detail="Job is not yet complete")

    safe_ticker = "".join(c for c in job.selections.ticker if c.isalnum() or c in "._-") or "ticker"
    filename = f"tradingagents_{safe_ticker}_{job.selections.analysis_date}.json"

    payload = {
        "job_id": job.id,
        "ticker": job.selections.ticker,
        "analysis_date": job.selections.analysis_date,
        "selections": job.selections.model_dump(),
        "decision": job.decision,
        "stats": job.stats,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "final_state": json_safe(job.final_state),
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/jobs/{job_id}/export.pdf")
def export_job_pdf(
    job_id: str, registry: JobRegistry = Depends(get_registry)
) -> Response:
    """Render a typeset PDF report of a completed analysis.

    Renders via WeasyPrint (HTML/CSS → PDF) — the Docker image ships
    the GTK runtime libs WeasyPrint needs. Host installs without GTK
    will see a 500 with a pointed remediation message.

    Available for any job whose `partial_state` has at least one
    populated section, so both in-flight (post-completion) and
    DB-loaded historical runs work.
    """
    job = registry.get(job_id) or _load_job_from_db(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not (job.partial_state or {}):
        raise HTTPException(
            status_code=409,
            detail="Job has no rendered reports yet — wait for it to complete.",
        )

    try:
        from webui.pdf_export import render_job_to_pdf
        pdf_bytes = render_job_to_pdf(job)
    except RuntimeError as e:
        # WeasyPrint missing (host install without GTK) — surface a
        # 500 with the install hint baked in.
        logging.getLogger("tradingagents.webui").exception("PDF export failed")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logging.getLogger("tradingagents.webui").exception("PDF export failed")
        raise HTTPException(status_code=500, detail=f"PDF render failed: {e}")

    safe_ticker = "".join(c for c in job.selections.ticker if c.isalnum() or c in "._-") or "ticker"
    filename = f"tradingagents_{safe_ticker}_{job.selections.analysis_date}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- helpers ----------

def _models_for_provider(provider: str) -> dict:
    """Return the quick/deep model option lists for a provider.

    Falls back to a single 'Custom model ID' entry when the provider has
    no curated catalog (e.g. azure, openrouter without explicit entries).
    """
    provider = provider.lower()
    catalog = MODEL_OPTIONS.get(provider)
    if catalog:
        return {"quick": catalog.get("quick", []), "deep": catalog.get("deep", [])}
    return {
        "quick": [("Custom model ID", "custom")],
        "deep": [("Custom model ID", "custom")],
    }


def _effort_html(request: Request, provider: str, selected: str = "") -> str:
    """Render the provider-specific effort/thinking dropdown (or empty string).

    `selected` preselects a matching option on initial form render. The
    HTMX swap on provider change calls this without `selected`, so the
    "Default (don't set)" option (always first) wins after a switch.
    """
    provider = provider.lower()
    # First option is always "Default (don't set)" → empty string → server
    # treats as None and doesn't forward the kwarg to the client. This
    # avoids 400 errors from models that reject the param (e.g. Anthropic
    # Sonnet/Haiku families reject `effort`; some OpenAI models reject
    # `reasoning_effort`). Users who want to override the model default
    # explicitly pick low/medium/high.
    none_choice = ("Default (don't set)", "")
    if provider == "openai":
        field = "openai_reasoning_effort"
        choices = [none_choice, ("Medium", "medium"), ("High", "high"), ("Low", "low")]
        label = "Reasoning effort"
    elif provider == "anthropic":
        field = "anthropic_effort"
        choices = [none_choice, ("High", "high"), ("Medium", "medium"), ("Low", "low")]
        label = "Effort level"
    elif provider == "google":
        field = "google_thinking_level"
        choices = [none_choice, ("Enable Thinking", "high"), ("Minimal", "minimal")]
        label = "Thinking mode"
    else:
        return ""  # nothing to configure
    tpl = request.app.state.templates.get_template("partials/effort.html")
    return tpl.render(
        request=request, field_name=field, label=label,
        choices=choices, selected=selected,
    )


def _load_job_from_db(job_id: str):
    """Reconstruct a `JobState` from the persisted `runs` row for the
    detail page. Returns None when DB is off, ``job_id`` isn't a UUID,
    or no matching row exists.

    The reconstructed JobState is read-only by construction — `current_agent`,
    `completed_agents`, `cancel_requested` etc. stay at their defaults,
    so the Stop button doesn't render and polling stops on the first
    fragment fetch (terminal status).
    """
    try:
        run_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return None

    try:
        from tradingagents.persistence import Run, session_scope
        from webui.models import RunSelections
        from webui.runner import JobState
    except Exception:  # noqa: BLE001
        return None

    try:
        with session_scope() as s:
            if s is None:
                return None
            run = s.get(Run, run_uuid)
            if run is None:
                return None

            partial = {r.section: r.content for r in run.reports}
            usage = run.token_usage.usage if run.token_usage else {}
            try:
                selections = RunSelections.model_validate(run.selections or {})
            except Exception:  # noqa: BLE001
                # Selections schema may have evolved since the row was
                # written. Synthesize a minimal stand-in so the heading
                # renders without failing the page.
                selections = RunSelections(
                    ticker=run.ticker,
                    analysis_date=run.analysis_date.isoformat(),
                    analysts=["market"],
                    llm_provider="openai",
                    quick_thinker="custom",
                    deep_thinker="custom",
                )

            return JobState(
                id=str(run.id),
                selections=selections,
                status=run.status,
                company_name=run.company_name,
                partial_state=partial,
                final_state=run.raw_state,
                decision=run.decision,
                error=run.error,
                traceback=run.traceback,
                started_at=run.started_at,
                finished_at=run.finished_at,
                stats=dict(usage) if isinstance(usage, dict) else {},
                db_run_id=run.id,
            )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("tradingagents.webui").warning(
            "DB job load failed for %s: %s", job_id, e,
        )
        return None


def _load_batch_from_db(batch_id: str):
    """Reconstruct a `RecentBatchRow` from Postgres for the detail page.

    Returns None when DB is off, ``batch_id`` isn't a UUID, or no
    matching row exists. The returned object has the same template
    attribute surface as `BatchJob` (via the `tickers` / `progress`
    properties on `RecentBatchRow`), so the existing
    `partials/batch_status.html` renders it unchanged.
    """
    try:
        bid = uuid.UUID(batch_id)
    except (ValueError, AttributeError):
        return None
    try:
        from tradingagents.persistence.batches import get_batch_db
        return get_batch_db(bid)
    except Exception:  # noqa: BLE001
        return None


def _merged_recent_batches(batch_reg, limit: int = 20) -> list:
    """Hybrid recent-batches view, decision #9 (registry wins on dedupe).

    In-memory registry holds live state (`running`, `paused`, `queued`
    + freshly-completed batches the process owns). DB holds historical
    rows + any from previous processes. Dedupe by `db_batch_id`;
    registry wins.
    """
    from tradingagents.persistence.batches import list_batches_db

    registry_rows = list(batch_reg.list_batches())
    seen_db_ids = {
        str(b.db_batch_id) for b in registry_rows
        if getattr(b, "db_batch_id", None) is not None
    }
    db_rows = [
        r for r in list_batches_db(limit=limit * 2)
        if str(r.id) not in seen_db_ids
    ]
    merged = registry_rows + db_rows
    merged.sort(key=lambda b: _normalize_ts(b.submitted_at), reverse=True)
    return merged[:limit]


def _normalize_ts(dt: Optional[datetime.datetime]) -> datetime.datetime:
    """Coerce any datetime (or None) to an aware UTC datetime for safe
    cross-source sorting.

    In-memory `JobState` / `BatchJob` timestamps come from
    `datetime.now()` and are naive; DB-loaded `RecentRunRow` /
    `RecentBatchRow` timestamps come back as aware (Postgres
    timestamptz). Python refuses to compare those directly, so the
    hybrid merge sorts use this helper to land everything in the same
    timezone space.
    """
    if dt is None:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


_ACTIVE_STATUSES = {"queued", "running"}
_TERMINAL_TODAY_CAP = 10
_HISTORY_CAP = 100
# Single DB pull funds both Recent jobs (today's terminals) and History
# (everything older). Sized to cover the History cap with headroom; the
# alternative was two queries on the same request.
_TERMINAL_FETCH = 500


def _is_today_local(dt: Optional[datetime.datetime]) -> bool:
    """True if `dt` falls on the server's local calendar day.

    `_normalize_ts` makes timestamps UTC-aware; `.astimezone()` with no
    arg converts to the host's local zone, which on dev (PST) matches
    what the user sees in the table after the JS rewrites `local-ts`.
    On a UTC-only Docker host the two collapse to the same day.
    """
    if dt is None:
        return False
    return _normalize_ts(dt).astimezone().date() == datetime.datetime.now().date()


def _registry_db_ids(rows) -> set:
    """`db_run_id`s already present in the registry — used to suppress
    duplicate DB rows when merging the two sources."""
    return {
        str(r.db_run_id) for r in rows
        if getattr(r, "db_run_id", None) is not None
    }


def _partition_jobs(registry: JobRegistry) -> tuple:
    """Hybrid view of in-memory registry + DB, split into three buckets.

    In-memory `JobRegistry` is the source of truth for in-flight state
    (`running`, `queued`, plus any terminals that haven't yet been
    flushed to DB by the runner). DB is canonical for older terminal
    rows. Dedupe by `db_run_id` — registry wins on collision because it
    holds live progress (`current_agent`, `sections_done()`).

    Returns ``(active, terminal_today, older)``; each list is sorted
    newest-first. Elements are heterogeneous (`JobState` or
    `RecentRunRow`) but share the attribute surface the template reads.
    """
    from tradingagents.persistence.runs import list_recent_terminal_runs

    registry_rows = list(registry.list_jobs())
    seen = _registry_db_ids(registry_rows)
    db_rows = [
        r for r in list_recent_terminal_runs(limit=_TERMINAL_FETCH)
        if str(r.id) not in seen
    ]

    # Drop error runs older than the TTL so they stop cluttering History.
    # The DB rows are already filtered in list_recent_terminal_runs; this
    # covers in-memory registry jobs from a long-lived process. Same TTL.
    from tradingagents.persistence.runs import ERROR_RUN_TTL
    error_cutoff = datetime.datetime.now(datetime.timezone.utc) - ERROR_RUN_TTL

    active, terminal_today, older = [], [], []
    for j in registry_rows:
        if j.status in _ACTIVE_STATUSES:
            active.append(j)
            continue
        if j.status == "error" and _normalize_ts(j.started_at) < error_cutoff:
            continue
        if _is_today_local(j.started_at):
            terminal_today.append(j)
        else:
            older.append(j)
    for r in db_rows:
        (terminal_today if _is_today_local(r.started_at) else older).append(r)

    key = lambda j: _normalize_ts(j.started_at)  # noqa: E731
    active.sort(key=key, reverse=True)
    terminal_today.sort(key=key, reverse=True)
    older.sort(key=key, reverse=True)
    return active, terminal_today, older


def _row_ticker(row) -> str:
    """Ticker symbol for a history row, whichever shape it has.

    `RecentRunRow` carries a typed `.ticker`; `JobState` nests it on
    `.selections` (a `RunSelections` model — or, for DB-reconstructed
    jobs with an evolved schema, possibly a plain dict).
    """
    ticker = getattr(row, "ticker", None)
    if not ticker:
        sel = getattr(row, "selections", None)
        ticker = getattr(sel, "ticker", None)
        if not ticker and isinstance(sel, dict):
            ticker = sel.get("ticker")
    return str(ticker or "?").strip().upper()


def _group_history(rows: list) -> list:
    """Bucket history rows by ticker for the expandable History table.

    Returns ``[{ticker, company_name, rows, count, latest}, ...]``.
    `rows` arrive newest-first from `_partition_jobs`, so each group's
    first row is its latest run and dict insertion order puts the most
    recently active ticker first — one pass, no re-sort.
    """
    groups: dict = {}
    for row in rows:
        ticker = _row_ticker(row)
        g = groups.get(ticker)
        if g is None:
            g = groups[ticker] = {
                "ticker": ticker,
                "company_name": None,
                "rows": [],
                "latest": row,
            }
        g["rows"].append(row)
        if not g["company_name"]:
            g["company_name"] = getattr(row, "company_name", None)
    out = list(groups.values())
    for g in out:
        g["count"] = len(g["rows"])
    return out


def _available_providers() -> list:
    """Subset of PROVIDERS whose API-key env var is set (or doesn't need one).

    Ollama has no API key requirement so it's always included. If the user
    has zero keys configured, returns just Ollama so the form is never
    empty — index() surfaces a warning in that case.
    """
    out = []
    for entry in PROVIDERS:
        _display, key, _url = entry
        env_var = get_api_key_env(key)
        if env_var is None:  # ollama → no key needed
            out.append(entry)
            continue
        if os.environ.get(env_var):
            out.append(entry)
    return out


def _total_agents_for(analyst_keys: list[str]) -> int:
    """Sum of analyst nodes + the fixed researcher/risk/trader/PM team."""
    # Fixed agents: Bull + Bear + Research Manager + Trader +
    # Aggressive + Conservative + Neutral + Portfolio Manager = 8
    return len(analyst_keys) + 8


_company_name_cache: dict[str, Optional[str]] = {}


def _lookup_company_name(ticker: str) -> Optional[str]:
    """Best-effort full-name lookup via yfinance, cached per-ticker.

    The yfinance `.info` call is a network round-trip; cache the result
    so repeated keypresses on the same ticker don't re-hit Yahoo. Negative
    results (unknown ticker) are also cached as None so we don't pound
    the API for a typo.
    """
    upper = ticker.strip().upper()
    if not upper:
        return None
    if upper in _company_name_cache:
        return _company_name_cache[upper]
    try:
        info = yf.Ticker(upper).info or {}
        name = info.get("longName") or info.get("shortName") or None
    except Exception as e:  # noqa: BLE001
        logging.getLogger("tradingagents.webui").debug(
            "Company-name lookup failed for %s: %s", upper, e,
        )
        name = None
    _company_name_cache[upper] = name
    return name


# `_json_safe` lived here before the cache module existed — now imported
# as `json_safe` from webui.cache so save_cache and export_job_json share
# the same recursion. This stub preserves the symbol name for any
# external import that hasn't been updated.
_json_safe = json_safe
