"""Unit tests for ticker-aware global news fetching (yfinance vendor).

Covers the two behaviors added together:

* `_queries_for_ticker` — Canadian listings (.TO / .V) route to
  `global_news_queries_canada`; everything else gets the default set.
* Per-query allocation — `get_global_news_yfinance` takes up to
  `global_news_articles_per_query` fresh articles per query until
  `global_news_article_limit` is reached, instead of letting the first
  query fill the whole quota.
"""

from types import SimpleNamespace

import pytest

from tradingagents.dataflows import yfinance_news
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.news_queries import queries_for_ticker
from tradingagents.dataflows.yfinance_news import get_global_news_yfinance

pytestmark = pytest.mark.unit


_CONFIG_KEYS = [
    "global_news_queries",
    "global_news_queries_canada",
    "global_news_article_limit",
    "global_news_articles_per_query",
    "global_news_lookback_days",
]


@pytest.fixture
def news_config():
    """Snapshot and restore the global-news config keys around each test."""
    saved = {k: get_config().get(k) for k in _CONFIG_KEYS}
    yield
    set_config(saved)


class _FakeSearch:
    def __init__(self, news):
        self.news = news


def _article(title: str) -> dict:
    # Flat article structure (the simpler of the two shapes the parser handles).
    return {"title": title, "publisher": "TestWire", "link": ""}


def _install_fake_search(monkeypatch, responses_for_query):
    """Replace yf.Search + yf_retry; returns the list of queries issued."""
    calls = []

    def fake_search(query, news_count, enable_fuzzy_query):
        calls.append(query)
        return _FakeSearch(responses_for_query(query, news_count))

    monkeypatch.setattr(yfinance_news, "yf", SimpleNamespace(Search=fake_search))
    monkeypatch.setattr(yfinance_news, "yf_retry", lambda fn: fn())
    return calls


class TestQueriesForTicker:
    def test_canadian_suffixes_route_to_canada_set(self, news_config):
        set_config({
            "global_news_queries": ["default"],
            "global_news_queries_canada": ["canada"],
        })
        config = get_config()
        assert queries_for_ticker("RY.TO", config) == ["canada"]
        assert queries_for_ticker("WEED.V", config) == ["canada"]
        # Case-insensitive, whitespace-tolerant
        assert queries_for_ticker(" ry.to ", config) == ["canada"]

    def test_non_canadian_and_missing_ticker_use_default(self, news_config):
        set_config({
            "global_news_queries": ["default"],
            "global_news_queries_canada": ["canada"],
        })
        config = get_config()
        assert queries_for_ticker("NVDA", config) == ["default"]
        assert queries_for_ticker("7203.T", config) == ["default"]
        assert queries_for_ticker(None, config) == ["default"]
        assert queries_for_ticker("", config) == ["default"]

    def test_falls_back_to_default_when_canada_set_empty(self, news_config):
        set_config({
            "global_news_queries": ["default"],
            "global_news_queries_canada": [],
        })
        assert queries_for_ticker("RY.TO", get_config()) == ["default"]


class TestPerQueryAllocation:
    def test_limit_spread_across_queries(self, monkeypatch, news_config):
        calls = _install_fake_search(
            monkeypatch,
            lambda q, n: [_article(f"{q} story {i}") for i in range(n)],
        )
        set_config({
            "global_news_queries": ["q1", "q2", "q3", "q4", "q5"],
            "global_news_article_limit": 6,
            "global_news_articles_per_query": 2,
        })
        out = get_global_news_yfinance("2026-06-06")
        # 2 articles per query -> the limit of 6 is reached after 3 queries,
        # not after the first one.
        assert calls == ["q1", "q2", "q3"]
        for q in ("q1", "q2", "q3"):
            assert f"{q} story 0" in out
            assert f"{q} story 1" in out
        assert "q4" not in out

    def test_duplicates_dont_count_against_per_query_take(self, monkeypatch, news_config):
        # Every query returns the same headlines plus one unique story; the
        # dedup must keep consulting later queries for fresh content.
        calls = _install_fake_search(
            monkeypatch,
            lambda q, n: [_article("shared headline"), _article(f"{q} unique")],
        )
        set_config({
            "global_news_queries": ["q1", "q2", "q3"],
            "global_news_article_limit": 4,
            "global_news_articles_per_query": 2,
        })
        out = get_global_news_yfinance("2026-06-06")
        assert calls == ["q1", "q2", "q3"]
        assert out.count("shared headline") == 1
        for q in ("q1", "q2", "q3"):
            assert f"{q} unique" in out

    def test_canadian_ticker_uses_canada_queries(self, monkeypatch, news_config):
        calls = _install_fake_search(
            monkeypatch,
            lambda q, n: [_article(f"{q} story")],
        )
        set_config({
            "global_news_queries": ["us macro"],
            "global_news_queries_canada": ["boc rates", "tsx market"],
            "global_news_article_limit": 4,
            "global_news_articles_per_query": 2,
        })
        out = get_global_news_yfinance("2026-06-06", ticker="RY.TO")
        assert calls == ["boc rates", "tsx market"]
        assert "boc rates story" in out
        assert "us macro" not in out

    def test_default_config_has_split_query_sets(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["global_news_queries"], "default macro set must be non-empty"
        assert DEFAULT_CONFIG["global_news_queries_canada"], "canada set must be non-empty"
        # The split must be real: no Canada-specific queries left in the default set.
        assert not any(
            "canada" in q.lower() or "canadian" in q.lower()
            for q in DEFAULT_CONFIG["global_news_queries"]
        )
        assert DEFAULT_CONFIG["global_news_articles_per_query"] >= 1
