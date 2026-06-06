"""FastAPI application factory for the TradingAgents web UI."""

from __future__ import annotations

import datetime as _dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md_lib
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup


_HERE = Path(__file__).resolve().parent


def _emit_time(dt, css_class: str, fmt: str) -> Markup:
    """Render a `<time>` element with a class the base.html JS hook
    recognizes. Naive datetimes are treated as UTC (Docker default +
    the convention we use server-side). The fallback text is what
    shows before JS runs / when JS is disabled.
    """
    if dt is None or dt == "":
        return Markup("")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return Markup(
        f'<time class="{css_class}" datetime="{dt.isoformat()}">'
        f'{dt.strftime(fmt)}</time>'
    )


def _local_dt_filter(dt) -> Markup:
    """ISO 8601 date only (YYYY-MM-DD) in the browser's local zone."""
    return _emit_time(dt, "local-ts", "%Y-%m-%d")


def _local_dt_full_filter(dt) -> Markup:
    """ISO 8601 date + 24h time (YYYY-MM-DD HH:MM:SS) in browser local
    zone. Used by Recent jobs / History Started columns."""
    return _emit_time(dt, "local-ts-full", "%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the schedule ticker when the server actually serves.

    Lifespan (not module import) so constructing the app in tests
    doesn't spin a background thread that fires the developer's real
    schedules. The thread is a daemon — no teardown needed.
    """
    from webui.schedules import get_schedule_registry  # local: avoid import cycle
    get_schedule_registry().ensure_ticker_started()
    yield


def create_app() -> FastAPI:
    """Construct the FastAPI app, mount static files, and register routes."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="TradingAgents",
        description="Multi-Agents LLM Financial Trading Framework — Web UI",
        version="0.3.0",
        lifespan=_lifespan,
    )

    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    templates = Jinja2Templates(directory=_HERE / "templates")
    # Markdown filter, used by partials/report.html. extensions=fenced_code/tables
    # keep the analyst reports' code blocks and tables rendering as expected.
    templates.env.filters["markdown"] = lambda text: md_lib.markdown(
        text or "", extensions=["fenced_code", "tables", "nl2br"]
    )
    # local_dt filter — emits a <time> tag with ISO 8601 + UTC offset.
    # Server-side rendering uses UTC (Docker default); a tiny JS hook in
    # base.html rewrites every `.local-ts` element to the browser's local
    # timezone on load + after every HTMX swap so PST users don't see
    # "00:23:00" when their wall clock reads "16:23".
    templates.env.filters["local_dt"] = _local_dt_filter
    templates.env.filters["local_dt_full"] = _local_dt_full_filter
    app.state.templates = templates

    # Local import to avoid a circular reference at module-import time.
    from webui.routes import router  # noqa: WPS433
    app.include_router(router)

    return app
