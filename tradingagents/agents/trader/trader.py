"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_plan = state["investment_plan"]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the execution trader on a multi-agent research team. "
                    "The Research Manager's investment plan is your mandate — do not "
                    "re-litigate the research; your value-add is execution parameters "
                    "and risk controls. Anchor every parameter in the plan's reasoning."
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the investment plan for {company_name}, produced by the "
                    f"research team from technical, fundamental, news, and sentiment "
                    f"analysis. {instrument_context}\n\n"
                    f"<investment_plan>\n{investment_plan}\n</investment_plan>\n\n"
                    f"Translate this plan into a concrete transaction proposal: direction, "
                    f"conviction level, entry approach (immediate vs. staged), "
                    f"position-sizing rationale, stop/invalidation level, and time horizon."
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
