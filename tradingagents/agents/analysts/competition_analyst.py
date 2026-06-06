from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_global_news,
    get_language_instruction,
    get_news,
)


def create_competition_analyst(llm):
    """Build the Competition Analyst node.

    The analyst identifies the target's most strategically relevant
    competitors (mostly from the LLM's training knowledge — competitor
    sets are widely covered), then optionally calls `get_news` against
    each competitor's ticker to enrich each entry with current
    developments. Output is a single markdown report with competitors
    ranked in declining order of strategic relevance.
    """

    def competition_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_news,         # call against each competitor's ticker for fresh context
            get_global_news,  # industry-level macro context
        ]

        system_message = (
            "You are a competitive-intelligence analyst. For the target instrument, "
            "identify the most strategically relevant competitors, rank them in "
            "declining order of relevance to a portfolio manager who is evaluating "
            "the target, and write exactly one paragraph per competitor explaining "
            "why that competitor is interesting *right now*.\n\n"
            "Process:\n"
            "  1. Use your training knowledge to enumerate likely competitors "
            "(public companies whenever possible — name and ticker).\n"
            "  2. Call `get_news` against the ticker symbols of the top 2-3 "
            "ranked competitors to confirm or surface very recent moves "
            "(earnings, products, regulatory actions, leadership changes); "
            "skip it for the tail of the list unless freshness would "
            "materially change the ranking or the takeaway.\n"
            "  3. Limit the list to the 4-6 most relevant competitors. Quality and "
            "ranking matter more than coverage.\n\n"
            "Output a single markdown report with the following structure:\n\n"
            "### Competitive landscape\n"
            "One paragraph framing the market structure, the axes of differentiation, "
            "and the most relevant recent shifts. No bullet points here.\n\n"
            "### Top competitors (ranked, most relevant first)\n"
            "For each competitor, use a level-4 heading of the form "
            "`#### 1. Competitor Name (TICKER)` (numbered, with ticker when "
            "publicly listed). Below each heading, write exactly ONE paragraph "
            "of 4-7 sentences explaining: where they overlap with the target, "
            "where they diverge, and why this competitor matters right now. "
            "End that paragraph with a single trailing line: "
            "`Threat level: low | medium | high.`\n\n"
            "### Summary table\n"
            "A markdown table with columns | # | Competitor | Ticker | Threat | "
            "One-line takeaway | summarising the ranked list."
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are one analyst on a multi-agent trading research team. Your report"
                    " will be read by bull/bear researchers and a portfolio manager downstream."
                    " Produce your analyst report only — do not recommend buy, sell, or hold;"
                    " that decision belongs to agents downstream."
                    " You have access to the following tools: {tool_names}.\n{system_message}\n"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "competition_report": report,
        }

    return competition_analyst_node
