"""newsdata.io-based news fetching (https://newsdata.io).

Implements the ``get_global_news`` vendor contract using newsdata.io's
``/api/1/market`` endpoint (via the official ``newsdataapi`` client) —
a market/finance-scoped feed with proper query/country/language
filtering, considerably better relevance than yfinance's fuzzy Search.
Note the market endpoint does NOT accept the ``category`` parameter
(it is already market-scoped).

Rate-limit handling is deliberately fail-fast. The official client,
on a 429, sleeps the server's ``Retry-After`` (commonly several
*minutes* on the free tier) *between* attempts and only raises once
``attempt >= max_retries``. With ``max_retries=1`` it raises
``NewsdataRateLimitError`` on the very first 429 with no sleep — we
catch it, stop querying, and let the analyst proceed with whatever it
has. Retrying within a single run is pointless anyway: the rate window
won't reset before the run finishes. (Trade-off: no retry on a
transient network blip either, which is an acceptable price for never
freezing an analyst tool call for minutes.)

Free-tier constraints honoured here:

* ``content`` / ``sentiment`` / ``ai_*`` fields are paid-plan only —
  the formatter uses title + description + link.
* Max 10 articles per request (``size``).
* Results may carry ``duplicate: true`` — those are skipped on top of
  the API-side ``removeduplicate=True``.

Requires ``NEWSDATA_API_KEY`` in the environment (loaded from ``.env``
by the package init). When the key is missing, the fetcher logs a
warning and delegates to the yfinance implementation so a
misconfigured deployment degrades instead of erroring.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from dateutil.relativedelta import relativedelta
from newsdataapi import NewsDataApiClient, NewsdataRateLimitError

from .config import get_config
from .news_queries import is_canadian_listing, queries_for_ticker

log = logging.getLogger(__name__)

# 1 = a single attempt, so a 429 raises NewsdataRateLimitError immediately
# instead of sleeping the server's multi-minute Retry-After before retrying.
# The client defaults to 5; see the module docstring for the rationale.
_MAX_RETRIES = 1
_REQUEST_TIMEOUT = 15  # seconds
_FREE_TIER_MAX_SIZE = 10  # articles per request on the free plan


def get_api_key() -> Optional[str]:
    """API key for newsdata.io, or None when unconfigured."""
    return os.getenv("NEWSDATA_API_KEY")


def get_global_news_newsdata(
    curr_date: str,
    look_back_days: Optional[int] = None,
    limit: Optional[int] = None,
    ticker: Optional[str] = None,
) -> str:
    """
    Retrieve global/macro economic news via the newsdata.io market-news API.

    Same contract and per-query allocation as the yfinance implementation:
    queries are consulted in priority order, taking up to
    ``global_news_articles_per_query`` fresh articles from each until
    ``limit`` is reached. Canadian listings (.TO / .V / .CN / .NE) use
    the Canada-focused query set and the ``country_canada`` country
    filter.

    Args:
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config. (The
            /latest endpoint only covers ~48 h, so this acts as an upper
            bound, not a guarantee of depth.)
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.
        ticker: Instrument being analyzed; routes regional coverage.

    Returns:
        Formatted string containing global news articles
    """
    api_key = get_api_key()
    if not api_key:
        log.warning(
            "NEWSDATA_API_KEY is not set — falling back to yfinance for global news"
        )
        from .yfinance_news import get_global_news_yfinance
        return get_global_news_yfinance(curr_date, look_back_days, limit, ticker)

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    per_query = config.get("global_news_articles_per_query", 2)
    search_queries = queries_for_ticker(ticker, config)

    params_cfg = config.get("newsdata_params", {})
    country = (
        params_cfg.get("country_canada", "ca,us")
        if is_canadian_listing(ticker)
        else params_cfg.get("country", "us,gb")
    )

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)

    client = NewsDataApiClient(
        apikey=api_key,
        max_retries=_MAX_RETRIES,
        request_timeout=_REQUEST_TIMEOUT,
    )

    articles = []
    seen_titles = set()
    errors = []

    for query in search_queries:
        if len(articles) >= limit:
            break

        try:
            # No `category` — the /1/market endpoint rejects it
            # (UnsupportedParameter); the feed is already market-scoped.
            payload = client.market_api(
                q=query,
                language=params_cfg.get("language", "en"),
                country=country,
                removeduplicate=True,
                size=min(_FREE_TIER_MAX_SIZE, per_query + 2),
            )
        except NewsdataRateLimitError as e:
            # Retries (with Retry-After) already happened inside the
            # client — further queries would just burn time. Stop here
            # and report what we have.
            errors.append(f"rate limited at {query!r}: {e}")
            break
        except Exception as e:  # noqa: BLE001 — degrade per query, not per call
            errors.append(f"{query!r}: {e}")
            continue

        if payload.get("status") != "success":
            errors.append(f"{query!r}: {payload.get('results', payload)}")
            continue

        taken = 0
        for item in payload.get("results", []):
            if taken >= per_query or len(articles) >= limit:
                break
            if item.get("duplicate"):
                continue
            title = (item.get("title") or "").strip()
            if not title or title in seen_titles:
                continue

            # pubDate is "YYYY-MM-DD HH:MM:SS" (UTC). Guard against
            # look-ahead and stale articles outside the lookback window.
            pub_date = item.get("pubDate") or ""
            if pub_date:
                try:
                    pub_dt = datetime.strptime(pub_date, "%Y-%m-%d %H:%M:%S")
                    if pub_dt > curr_dt + relativedelta(days=1) or pub_dt < start_dt:
                        continue
                except ValueError:
                    pass  # unparseable date — keep the article

            seen_titles.add(title)
            articles.append({
                "title": title,
                "description": (item.get("description") or "").strip(),
                "source": item.get("source_name") or item.get("source_id") or "Unknown",
                "link": item.get("link") or "",
                "pub_date": pub_date,
            })
            taken += 1

    if not articles:
        detail = f" (errors: {'; '.join(errors)})" if errors else ""
        return f"No global news found for {curr_date} via newsdata.io{detail}"

    start_date = start_dt.strftime("%Y-%m-%d")
    news_str = ""
    for a in articles[:limit]:
        dated = f", {a['pub_date']}" if a["pub_date"] else ""
        news_str += f"### {a['title']} (source: {a['source']}{dated})\n"
        if a["description"]:
            news_str += f"{a['description']}\n"
        if a["link"]:
            news_str += f"Link: {a['link']}\n"
        news_str += "\n"

    return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"
