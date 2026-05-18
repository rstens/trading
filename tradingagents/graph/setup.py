# TradingAgents/graph/setup.py

from typing import Any, Dict

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import *
from tradingagents.agents.utils.agent_states import AgentState

from .analyst_subgraph import build_analyst_subgraph, make_subgraph_node
from .conditional_logic import ConditionalLogic


# Map analyst-selector key → (builder fn, AgentState field the analyst writes to).
# The "social" key is kept for back-compat (it now drives sentiment_analyst).
_ANALYST_BUILDERS = {
    "market":       (create_market_analyst,       "market_report"),
    "social":       (create_sentiment_analyst,    "sentiment_report"),
    "news":         (create_news_analyst,         "news_report"),
    "fundamentals": (create_fundamentals_analyst, "fundamentals_report"),
    "competition":  (create_competition_analyst,  "competition_report"),
}


class GraphSetup:
    """Builds the LangGraph workflow for a TradingAgents run.

    Analyst phase: each selected analyst is wrapped as an isolated
    subgraph (see ``analyst_subgraph.py``) so they can run in parallel
    without their tool-loop messages colliding on the shared
    ``messages`` channel. The parent fan-outs from START to all selected
    analyst nodes; LangGraph's implicit barrier on multiple inbound
    edges to Bull Researcher waits for every analyst to finish before
    the debate phase begins.

    Researcher + risk + portfolio phases remain sequential — they have
    real causal dependencies on each other's outputs.
    """

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: Dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
    ):
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic

    def setup_graph(
        self, selected_analysts=["market", "social", "news", "fundamentals"]
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include.
                Options: "market", "social" (sentiment), "news",
                "fundamentals".
        """
        if not selected_analysts:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")
        unknown = [a for a in selected_analysts if a not in _ANALYST_BUILDERS]
        if unknown:
            raise ValueError(f"Unknown analyst selector(s): {unknown}")

        # Build a compiled subgraph per selected analyst, then wrap each
        # as a single parent-graph node. The wrapper gives the analyst an
        # isolated `messages` channel and returns only its report field.
        analyst_nodes: Dict[str, Any] = {}
        for analyst_key in selected_analysts:
            builder, report_field = _ANALYST_BUILDERS[analyst_key]
            analyst_node_fn = builder(self.quick_thinking_llm)
            tool_node = self.tool_nodes[analyst_key]
            subgraph = build_analyst_subgraph(analyst_node_fn, tool_node)
            analyst_nodes[analyst_key] = make_subgraph_node(subgraph, report_field)

        # Researcher / manager / trader / risk / portfolio chain — sequential.
        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)
        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        workflow = StateGraph(AgentState)

        # Analyst nodes. Names match the strings webui/runner.py::NODE_LABELS
        # checks against, so the live progress callback still picks them up.
        for analyst_key, node_fn in analyst_nodes.items():
            workflow.add_node(f"{analyst_key.capitalize()} Analyst", node_fn)

        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Fan-out: parallel start → each analyst subgraph runs concurrently.
        for analyst_key in selected_analysts:
            workflow.add_edge(START, f"{analyst_key.capitalize()} Analyst")

        # Fan-in: every analyst subgraph converges at Bull Researcher.
        # LangGraph applies an implicit barrier when a node has multiple
        # inbound edges — Bull Researcher waits for ALL analyst nodes
        # to complete before running.
        for analyst_key in selected_analysts:
            workflow.add_edge(f"{analyst_key.capitalize()} Analyst", "Bull Researcher")

        # Researcher debate (sequential — bull and bear take turns).
        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bear Researcher": "Bear Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bull Researcher": "Bull Researcher",
                "Research Manager": "Research Manager",
            },
        )

        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")

        # Risk debate (three-way round-robin).
        workflow.add_conditional_edges(
            "Aggressive Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Conservative Analyst": "Conservative Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Conservative Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Neutral Analyst": "Neutral Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Aggressive Analyst": "Aggressive Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
