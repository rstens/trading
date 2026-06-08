"""Tests for the /healthz liveness probe and the access-log noise filter."""

import logging

import pytest
from fastapi.testclient import TestClient

from webui.app import (
    _AccessLogPathFilter,
    _install_access_log_filter,
    create_app,
)

pytestmark = pytest.mark.unit


class TestHealthz:
    def test_returns_200_ok(self):
        r = TestClient(create_app()).get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_not_in_openapi_schema(self):
        schema = TestClient(create_app()).get("/openapi.json").json()
        assert "/healthz" not in schema["paths"]
        assert "/favicon.ico" not in schema["paths"]


def _record(path: str) -> logging.LogRecord:
    # Mirror uvicorn's access-log record shape:
    # args = (client_addr, method, path, http_version, status)
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d', args=("127.0.0.1:1", "GET", path, "1.1", 200),
        exc_info=None,
    )
    return rec


class TestAccessLogFilter:
    def test_drops_quiet_paths(self):
        f = _AccessLogPathFilter()
        assert f.filter(_record("/healthz")) is False
        assert f.filter(_record("/favicon.ico")) is False

    def test_passes_normal_paths(self):
        f = _AccessLogPathFilter()
        assert f.filter(_record("/")) is True
        assert f.filter(_record("/jobs/abc123")) is True
        assert f.filter(_record("/htmx/jobs/abc/status")) is True

    def test_tolerates_unexpected_record_shapes(self):
        f = _AccessLogPathFilter()
        rec = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__,
            lineno=1, msg="startup complete", args=(), exc_info=None,
        )
        assert f.filter(rec) is True  # no args → pass through

    def test_install_is_idempotent(self):
        logger = logging.getLogger("uvicorn.access")
        before = [f for f in logger.filters if isinstance(f, _AccessLogPathFilter)]
        for f in before:
            logger.removeFilter(f)
        _install_access_log_filter()
        _install_access_log_filter()
        _install_access_log_filter()
        installed = [f for f in logger.filters if isinstance(f, _AccessLogPathFilter)]
        assert len(installed) == 1

    def test_create_app_installs_the_filter(self):
        logger = logging.getLogger("uvicorn.access")
        for f in [f for f in logger.filters if isinstance(f, _AccessLogPathFilter)]:
            logger.removeFilter(f)
        create_app()
        assert any(isinstance(f, _AccessLogPathFilter) for f in logger.filters)
