from typing import Annotated, Optional

from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.tools import tool

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)


@tool
def search_past_analyses(
    query: str,
    ticker: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5,
) -> str:
    """Search the vector store for past analyses semantically similar to ``query``.

    Returns a markdown-formatted list of up to ``limit`` hits. Each hit
    is one previously-completed analysis whose ``summary`` section is
    closest to ``query`` in embedding space. Useful for: comparing current
    findings to historical analyses of the same instrument, surfacing
    industry/sector patterns, and sanity-checking thesis novelty.

    Args:
        query: Free-text description of what to look for (e.g. "AI chip
            cycle peak", "earnings beat reaction", "regulatory headline
            risk semiconductor"). The string is embedded and matched
            against the stored summary embeddings via cosine distance.
        ticker: Restrict results to a single ticker (e.g. "NVDA"). None
            means any ticker — useful for industry-context searches.
        since: ISO date string ``YYYY-MM-DD``. Only consider analyses
            with ``analysis_date >= since``. None means no date filter.
        limit: Top-K results to return (default 5). Capped at 25.

    Returns:
        A markdown list like:

            1. **NVDA** · 2026-04-15 · cos=0.21
               <first ~300 chars of summary>...

            2. **AMD** · 2026-04-12 · cos=0.34
               ...

        Empty string when no DB / vector store / matches.
    """
    # Local imports — kept inside the @tool so analyst modules that
    # don't use this tool aren't forced to import the persistence
    # package (which pulls in SQLAlchemy + langchain_openai).
    import datetime as _dt
    from tradingagents.persistence.embeddings import find_similar

    if not query or not query.strip():
        return ""
    limit = max(1, min(int(limit or 5), 25))

    since_date = None
    if since:
        try:
            since_date = _dt.date.fromisoformat(since.strip())
        except ValueError:
            return f"`search_past_analyses` error: invalid `since` value {since!r}; expected YYYY-MM-DD."

    hits = find_similar(
        query_text=query,
        ticker=(ticker or None),
        sections=["summary", "final_trade_decision", "investment_plan"],
        limit=limit,
        since=since_date,
    )
    if not hits:
        return ""

    lines = []
    for i, h in enumerate(hits, 1):
        excerpt = (h.content or "").strip()
        if len(excerpt) > 300:
            excerpt = excerpt[:300].rstrip() + "..."
        lines.append(
            f"{i}. **{h.ticker}** · {h.section} · cos={h.distance:.2f}\n   {excerpt}"
        )
    return "\n\n".join(lines)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Applied to every agent whose output reaches the saved report —
    analysts, researchers, debaters, research manager, trader, and
    portfolio manager — so a non-English run produces a fully localized
    report rather than a mix of languages.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
