"""Unit tests for the newsdata.io global-news vendor."""

from types import SimpleNamespace

import pytest

from tradingagents.dataflows import newsdata_io
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.newsdata_io import get_global_news_newsdata

pytestmark = pytest.mark.unit


_CONFIG_KEYS = [
    "global_news_queries",
    "global_news_queries_canada",
    "global_news_article_limit",
    "global_news_articles_per_query",
    "global_news_lookback_days",
    "newsdata_params",
]


@pytest.fixture
def news_config():
    """Snapshot and restore the global-news config keys around each test."""
    saved = {k: get_config().get(k) for k in _CONFIG_KEYS}
    yield
    set_config(saved)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("NEWSDATA_API_KEY", "pub_test_key")


def _item(title, *, duplicate=False, pub_date="2026-06-06 10:00:00"):
    return {
        "title": title,
        "description": f"{title} description",
        "link": f"https://example.com/{title.replace(' ', '-')}",
        "source_name": "TestWire",
        "pubDate": pub_date,
        "duplicate": duplicate,
    }


def _install_fake_client(monkeypatch, results_for_query, status="success"):
    """Replace NewsDataApiClient in the vendor module; returns captured kwargs."""
    captured = []

    class _FakeClient:
        def __init__(self, apikey, **kwargs):
            self.apikey = apikey

        def market_api(self, **kwargs):
            captured.append(kwargs)
            return {"status": status, "results": results_for_query(kwargs["q"])}

    monkeypatch.setattr(newsdata_io, "NewsDataApiClient", _FakeClient)
    return captured


class TestNewsdataGlobalNews:
    def test_per_query_allocation_and_formatting(self, monkeypatch, news_config, api_key):
        captured = _install_fake_client(
            monkeypatch,
            lambda q: [_item(f"{q} story {i}") for i in range(4)],
        )
        set_config({
            "global_news_queries": ["q1", "q2", "q3"],
            "global_news_article_limit": 4,
            "global_news_articles_per_query": 2,
        })
        out = get_global_news_newsdata("2026-06-06")
        assert [p["q"] for p in captured] == ["q1", "q2"]
        assert "q1 story 0" in out and "q2 story 1" in out
        assert "q1 story 0 description" in out
        assert "Link: https://example.com/q1-story-0" in out
        assert "q3" not in out

    def test_duplicates_and_stale_articles_skipped(self, monkeypatch, news_config, api_key):
        def results(q):
            return [
                _item("dup story", duplicate=True),
                _item("stale story", pub_date="2026-05-01 10:00:00"),  # outside lookback
                _item("future story", pub_date="2026-06-09 10:00:00"),  # look-ahead
                _item(f"{q} good story"),
            ]

        _install_fake_client(monkeypatch, results)
        set_config({
            "global_news_queries": ["q1"],
            "global_news_article_limit": 5,
            "global_news_articles_per_query": 5,
            "global_news_lookback_days": 7,
        })
        out = get_global_news_newsdata("2026-06-06")
        assert "good story" in out
        assert "dup story" not in out
        assert "stale story" not in out
        assert "future story" not in out

    def test_canadian_ticker_routes_queries_and_country(self, monkeypatch, news_config, api_key):
        captured = _install_fake_client(monkeypatch, lambda q: [_item(f"{q} story")])
        set_config({
            "global_news_queries": ["us macro"],
            "global_news_queries_canada": ["boc rates"],
            "global_news_article_limit": 2,
            "global_news_articles_per_query": 2,
            "newsdata_params": {
                "language": "en",
                "country": "us,gb",
                "country_canada": "ca,us",
            },
        })
        out = get_global_news_newsdata("2026-06-06", ticker="RY.TO")
        assert captured[0]["q"] == "boc rates"
        assert captured[0]["country"] == "ca,us"
        assert captured[0]["language"] == "en"
        # /1/market rejects the category parameter — it must never be sent.
        assert "category" not in captured[0]
        assert "boc rates story" in out

        captured.clear()
        get_global_news_newsdata("2026-06-06", ticker="NVDA")
        assert captured[0]["q"] == "us macro"
        assert captured[0]["country"] == "us,gb"

    def test_missing_api_key_falls_back_to_yfinance(self, monkeypatch, news_config):
        monkeypatch.delenv("NEWSDATA_API_KEY", raising=False)
        called = {}

        def fake_yf(curr_date, look_back_days, limit, ticker):
            called["args"] = (curr_date, look_back_days, limit, ticker)
            return "yfinance fallback output"

        import tradingagents.dataflows.yfinance_news as yf_news
        monkeypatch.setattr(yf_news, "get_global_news_yfinance", fake_yf)
        out = get_global_news_newsdata("2026-06-06", 7, 5, "RY.TO")
        assert out == "yfinance fallback output"
        assert called["args"] == ("2026-06-06", 7, 5, "RY.TO")

    def test_rate_limit_stops_remaining_queries(self, monkeypatch, news_config, api_key):
        from newsdataapi import NewsdataRateLimitError

        captured = []

        class _FakeClient:
            def __init__(self, apikey, **kwargs):
                pass

            def market_api(self, **kwargs):
                captured.append(kwargs["q"])
                if kwargs["q"] == "q2":
                    raise NewsdataRateLimitError("429 too many requests")
                return {"status": "success", "results": [_item(f"{kwargs['q']} story")]}

        monkeypatch.setattr(newsdata_io, "NewsDataApiClient", _FakeClient)
        set_config({
            "global_news_queries": ["q1", "q2", "q3"],
            "global_news_article_limit": 6,
            "global_news_articles_per_query": 1,
        })
        out = get_global_news_newsdata("2026-06-06")
        # q3 must not be attempted after the rate limit on q2; q1's article
        # is still returned.
        assert captured == ["q1", "q2"]
        assert "q1 story" in out

    def test_api_error_status_degrades_gracefully(self, monkeypatch, news_config, api_key):
        _install_fake_client(monkeypatch, lambda q: [], status="error")
        set_config({
            "global_news_queries": ["q1"],
            "global_news_article_limit": 2,
            "global_news_articles_per_query": 2,
        })
        out = get_global_news_newsdata("2026-06-06")
        assert "No global news found" in out

    def test_vendor_registered_for_global_news(self):
        from tradingagents.dataflows.interface import VENDOR_LIST, VENDOR_METHODS
        assert "newsdata" in VENDOR_LIST
        assert VENDOR_METHODS["get_global_news"]["newsdata"] is get_global_news_newsdata
        # newsdata implements only get_global_news — other news methods
        # must keep routing to the remaining vendors.
        assert "newsdata" not in VENDOR_METHODS["get_news"]
        assert "newsdata" not in VENDOR_METHODS["get_insider_transactions"]

    def test_default_config_routes_global_news_to_newsdata(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["tool_vendors"]["get_global_news"] == "newsdata"
        assert DEFAULT_CONFIG["newsdata_params"]["country_canada"].startswith("ca")
