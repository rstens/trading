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

# Request paths whose uvicorn access-log lines are suppressed — high-
# frequency, zero-information probes. The Docker healthcheck hits
# /healthz every 10 s; /favicon.ico is poked by bots and old clients.
_QUIET_ACCESS_PATHS = frozenset({"/healthz", "/favicon.ico"})


class _AccessLogPathFilter(logging.Filter):
    """Drop uvicorn access-log records for a set of noisy paths.

    uvicorn formats access logs with
    ``record.args = (client_addr, method, path, http_version, status)``,
    so the request path is the third positional arg. Anything we don't
    recognize is passed through untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and args[2] in _QUIET_ACCESS_PATHS:
            return False
        return True


def _install_access_log_filter() -> None:
    """Attach the path filter to uvicorn's access logger, once.

    Idempotent: create_app() runs per-process (and many times under the
    test client), so guard against stacking duplicate filters.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogPathFilter) for f in access_logger.filters):
        access_logger.addFilter(_AccessLogPathFilter())


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

    Also reconciles any `runs` left `queued`/`running` by a previous
    process (a restart killed their owning JobState): they're marked
    `error` so the UI sources a terminal status from the DB instead of
    polling a ghost forever. No-op when the DB is off.
    """
    try:
        from tradingagents.persistence import interrupt_in_flight_runs
        n = interrupt_in_flight_runs()
        if n:
            logging.getLogger("tradingagents.webui").info(
                "Startup: reconciled %d interrupted run(s)", n
            )
    except Exception as e:  # noqa: BLE001 — never block startup on this
        logging.getLogger("tradingagents.webui").warning(
            "Startup run reconciliation failed: %s", e
        )

    from webui.schedules import get_schedule_registry  # local: avoid import cycle
    get_schedule_registry().ensure_ticker_started()
    yield


def create_app() -> FastAPI:
    """Construct the FastAPI app, mount static files, and register routes."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _install_access_log_filter()

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
