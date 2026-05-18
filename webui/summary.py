"""LLM-generated trading summary that runs after the pipeline completes.

A single deep-thinker call that distills the full pipeline output into a
short, actionable markdown block with four sections: Trading Advice,
Boundaries, Entry Triggers, Exit Triggers. Surfaces as a final "Summary"
tab in the web UI.

Kept separate from the LangGraph pipeline so the summary failing never
poisons the canonical run state — at worst, the tab is just absent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from tradingagents.agents.utils.agent_utils import get_language_instruction
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.llm_clients import create_llm_client


log = logging.getLogger(__name__)


SUMMARY_PROMPT = """You are a senior trading desk synthesizer. The team has just finished a multi-agent analysis of {ticker} as of {trade_date}. Your job is to produce a concise, immediately actionable summary that a portfolio manager could hand to a trader.

Write a single markdown response with EXACTLY these four sections, in this order, using these headings verbatim:

## Trading Advice
One to three sentences. State the recommendation (BUY / HOLD / SELL plus conviction), the time horizon, and the single most important reason. No hedging.

## Boundaries
Bulleted list. Position-size cap (% of book or absolute), stop-loss level (price or % from entry), max acceptable drawdown, and any liquidity or concentration limit. Use concrete numbers when the source material provides them; otherwise mark "(not specified — apply standard {ticker} risk limits)".

## Entry Triggers
Bulleted list of *specific, observable* conditions that should trigger opening (or adding to) the position. Each bullet must be a measurable signal: price levels, technical thresholds, news catalysts, or earnings/macro events. Avoid vague guidance like "wait for confirmation".

## Exit Triggers
Bulleted list of conditions that should trigger closing (or trimming) the position — both for taking profit and for cutting losses. Include time-based exits if appropriate (e.g., "if the thesis hasn't played out by 30 trading days, reassess").

Be terse. Prefer numbers over adjectives. Do not restate the analysis; assume the reader has read it. If a section cannot be filled from the source material, write a single bullet "(insufficient input — escalate to analyst)" rather than inventing levels.{language_instruction}

---

FINAL TRADE DECISION:
{final_trade_decision}

INVESTMENT PLAN (from Research Manager):
{investment_plan}

TRADER'S PLAN:
{trader_investment_plan}

MARKET / TECHNICAL REPORT:
{market_report}

FUNDAMENTALS REPORT:
{fundamentals_report}

NEWS REPORT:
{news_report}

SENTIMENT REPORT:
{sentiment_report}

COMPETITION REPORT:
{competition_report}
"""


def _section(state: Dict[str, Any], key: str, fallback: str = "(not produced this run)") -> str:
    value = state.get(key)
    return value if value else fallback


def generate_summary(config: Dict[str, Any], final_state: Dict[str, Any]) -> str:
    """Run the summary LLM call. Returns markdown.

    Uses the deep-thinker model (this is synthesis, not a quick pass).
    Honors the run's `output_language` so the summary matches the rest of
    the user-facing reports.
    """
    # Push the run's config into the dataflows mirror temporarily so
    # `get_language_instruction()` picks up the right language for this
    # call (it reads from the module-global get_config()).
    set_config(config)

    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
    )
    llm = client.get_llm()

    language_instruction = get_language_instruction()
    prompt = SUMMARY_PROMPT.format(
        ticker=final_state.get("company_of_interest", "the instrument"),
        trade_date=final_state.get("trade_date", ""),
        final_trade_decision=_section(final_state, "final_trade_decision"),
        investment_plan=_section(final_state, "investment_plan"),
        trader_investment_plan=_section(final_state, "trader_investment_plan"),
        market_report=_section(final_state, "market_report"),
        fundamentals_report=_section(final_state, "fundamentals_report"),
        news_report=_section(final_state, "news_report"),
        sentiment_report=_section(final_state, "sentiment_report"),
        competition_report=_section(final_state, "competition_report"),
        language_instruction=language_instruction,
    )

    log.info("Generating trading summary for %s", final_state.get("company_of_interest", "?"))
    result = llm.invoke(prompt)
    content = getattr(result, "content", result)

    # langchain may return content as a list of typed blocks (Anthropic
    # extended thinking, OpenAI structured output, etc.) — flatten to text.
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        content = "".join(parts).strip()
    return str(content).strip()
