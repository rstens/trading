"""Vendor-neutral macro-news query selection.

`get_global_news` implementations (yfinance, newsdata.io) share the
same priority-ordered query lists from the config; which list applies
depends on the instrument being analyzed. Kept in its own module so no
vendor has to import another vendor's module for the routing logic.
"""

from typing import Optional

# Exchange suffixes that route get_global_news to the Canada-focused
# query set. Covers every Canadian exchange Yahoo Finance qualifies:
#   .TO — Toronto Stock Exchange (TSX)
#   .V  — TSX Venture Exchange (TSXV)
#   .CN — Canadian Securities Exchange (CSE)
#   .NE — Cboe Canada / NEO Exchange
CANADIAN_SUFFIXES = (".TO", ".V", ".CN", ".NE")


def is_canadian_listing(ticker: Optional[str]) -> bool:
    """True when ``ticker`` is a Canadian listing (.TO / .V / .CN / .NE)."""
    return bool(ticker) and ticker.strip().upper().endswith(CANADIAN_SUFFIXES)


def queries_for_ticker(ticker: Optional[str], config: dict) -> list:
    """Pick the macro query set for the instrument being analyzed.

    Canadian listings (.TO / .V / .CN / .NE) get
    ``global_news_queries_canada``; everything else — or no ticker —
    gets the default ``global_news_queries``.
    """
    if is_canadian_listing(ticker):
        canada_queries = config.get("global_news_queries_canada")
        if canada_queries:
            return canada_queries
    return config["global_news_queries"]
