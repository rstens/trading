from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
    get_language_instruction,
)
from tradingagents.dataflows.config import get_config


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
            get_insider_transactions,
        ]

        system_message = (
            "You are a fundamentals analyst examining the company's latest reported financials and any changes since the prior period.\n\n"
            "Tool usage:\n"
            "- Call `get_fundamentals` first for the comprehensive company overview — always retrieve it before writing.\n"
            "- Call `get_balance_sheet`, `get_cashflow`, or `get_income_statement` when a specific question (leverage, cash burn, margin trend) needs line-item evidence.\n"
            "- Call `get_insider_transactions` to check whether insiders are buying or selling.\n\n"
            "Report contents — analyze through these lenses:\n"
            "- Profitability: revenue growth, margin trends, earnings trajectory\n"
            "- Balance-sheet strength: leverage, liquidity, debt pressure\n"
            "- Cash generation: free cash flow vs. reported earnings (earnings quality)\n"
            "- Valuation: current multiples vs. the company's own history\n"
            "- Red flags: anything in the statements or insider activity a portfolio manager should know before sizing a position\n\n"
            "Ground every observation in specific figures from the retrieved statements."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
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
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
