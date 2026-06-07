"""Unit tests for the Hedging Agent node and its pipeline/UI wiring."""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.managers.hedging_agent import create_hedging_agent

pytestmark = pytest.mark.unit


def _make_state(**overrides):
    state = {
        "company_of_interest": "NVDA",
        "trade_date": "2026-06-06",
        "market_report": "RSI at 44, MACD fading.",
        "sentiment_report": "Retail crowded long.",
        "news_report": "Senate China export hearing pending.",
        "fundamentals_report": "Inventory rose to 31.6% of revenue.",
        "competition_report": "AMD closing the gap.",
        "investment_plan": "**Recommendation**: Overweight",
        "trader_investment_plan": "**Action**: Buy at 205, stop 188",
        "final_trade_decision": "**Rating**: Overweight — staged entry.",
        "risk_debate_state": {"history": "Conservative: gap risk through stops."},
    }
    state.update(overrides)
    return state


def _mock_llm(response_text="LOW weakness — no hedge needed."):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=response_text)
    return llm


class TestHedgingAgentNode:
    def test_writes_hedging_report(self):
        llm = _mock_llm("ELEVATED — collar recommended.")
        node = create_hedging_agent(llm)
        result = node(_make_state())
        assert result == {"hedging_report": "ELEVATED — collar recommended."}

    def test_prompt_contains_all_upstream_inputs(self):
        llm = _mock_llm()
        node = create_hedging_agent(llm)
        node(_make_state())
        prompt = llm.invoke.call_args[0][0]
        # Every upstream output is present, XML-tagged.
        assert "<market_report>\nRSI at 44, MACD fading." in prompt
        assert "<sentiment_report>" in prompt
        assert "<news_report>" in prompt
        assert "<fundamentals_report>" in prompt
        assert "<competition_report>\nAMD closing the gap." in prompt
        assert "<research_plan>" in prompt
        assert "<trader_proposal>" in prompt
        assert "<risk_debate>\nConservative: gap risk through stops." in prompt
        assert "<final_decision>" in prompt
        assert "`NVDA`" in prompt  # instrument context

    def test_missing_optional_inputs_dont_crash(self):
        llm = _mock_llm()
        node = create_hedging_agent(llm)
        result = node(_make_state(
            competition_report="", risk_debate_state=None,
        ))
        assert "hedging_report" in result
        prompt = llm.invoke.call_args[0][0]
        # The optional competition block is omitted entirely when empty.
        assert "<competition_report>" not in prompt


class TestPipelineWiring:
    def test_graph_includes_hedging_agent_after_portfolio_manager(self):
        from langgraph.graph import END
        from langgraph.prebuilt import ToolNode
        from tradingagents.agents.utils.agent_utils import (
            get_indicators,
            get_stock_data,
        )
        from tradingagents.graph.conditional_logic import ConditionalLogic
        from tradingagents.graph.setup import GraphSetup

        setup = GraphSetup(
            quick_thinking_llm=MagicMock(),
            deep_thinking_llm=MagicMock(),
            tool_nodes={"market": ToolNode([get_stock_data, get_indicators])},
            conditional_logic=ConditionalLogic(),
        )
        workflow = setup.setup_graph(["market"])
        assert "Hedging Agent" in workflow.nodes
        # Portfolio Manager feeds the Hedging Agent, which terminates the graph.
        edges = {(e[0], e[1]) for e in workflow.edges}
        assert ("Portfolio Manager", "Hedging Agent") in edges
        assert ("Hedging Agent", END) in edges

    def test_agent_state_has_hedging_field(self):
        from tradingagents.agents.utils.agent_states import AgentState
        assert "hedging_report" in AgentState.__annotations__


class TestUIWiring:
    def test_webui_tab_and_labels_registered(self):
        from webui.runner import (
            _FLAT_SECTION_FIELDS,
            NODE_LABELS,
            TAB_SECTIONS,
        )
        assert NODE_LABELS.get("Hedging Agent") == "Hedging Agent"
        assert ("hedging", "Hedging Agent", "hedging_report") in TAB_SECTIONS
        assert "hedging_report" in _FLAT_SECTION_FIELDS

    def test_cli_sections_registered(self):
        from cli.main import MessageBuffer
        assert MessageBuffer.REPORT_SECTIONS["hedging_report"] == (None, "Hedging Agent")
        assert "Hedging Agent" in MessageBuffer.FIXED_AGENTS["Portfolio Management"]
