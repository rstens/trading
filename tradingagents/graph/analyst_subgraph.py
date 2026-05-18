"""Per-analyst LangGraph subgraphs for fan-out parallel execution.

Each analyst (market, sentiment, news, fundamentals) is wrapped as a
self-contained subgraph with its own ``messages`` channel, then exposed
to the parent graph as a single node. The parent fan-outs from START to
all selected analyst subgraphs in parallel; LangGraph's implicit join on
multiple inbound edges to Bull Researcher waits until every analyst
subgraph has finished before the debate phase starts.

Why subgraphs and not just multiple parallel edges to the existing
analyst nodes:

The existing nodes share ``state["messages"]`` (the MessagesState's
single message channel with the ``add_messages`` reducer). If four
analysts ran in parallel against that shared channel, each analyst's
tool-loop iteration would see the *other three's* tool messages mixed
in — the LLM would get a garbled conversation and either crash or
hallucinate. Subgraphs give each analyst its own message namespace.

Internal layout per subgraph:

    START → analyst ⇄ tools → END

The subgraph wrapper (``make_subgraph_node``) returns ONLY the report
field to the parent — never the subgraph's internal ``messages`` — so
parallel branches don't pollute the parent's shared message channel.
"""

from __future__ import annotations

from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import AgentState


_TOOLS_BRANCH = "tools"
_DONE_BRANCH = "_done"


def _should_continue(state) -> str:
    """Common subgraph routing: more tool calls → loop; otherwise exit."""
    messages = state["messages"]
    if not messages:
        return _DONE_BRANCH
    last = messages[-1]
    if getattr(last, "tool_calls", None):
        return _TOOLS_BRANCH
    return _DONE_BRANCH


def build_analyst_subgraph(analyst_node: Callable, tool_node: ToolNode):
    """Compile a self-contained subgraph wrapping one analyst + its tools.

    The subgraph uses ``AgentState`` (same schema as the parent) so the
    analyst node can read all the run-context fields it expects
    (``company_of_interest``, ``trade_date``, ``past_context``, etc.).
    """
    sub = StateGraph(AgentState)
    sub.add_node("analyst", analyst_node)
    sub.add_node(_TOOLS_BRANCH, tool_node)

    sub.add_edge(START, "analyst")
    sub.add_conditional_edges(
        "analyst",
        _should_continue,
        {_TOOLS_BRANCH: _TOOLS_BRANCH, _DONE_BRANCH: END},
    )
    sub.add_edge(_TOOLS_BRANCH, "analyst")
    return sub.compile()


def make_subgraph_node(subgraph, report_field: str):
    """Wrap a compiled analyst subgraph as a single parent-graph node.

    The wrapper:

    1. Builds a fresh initial state for the subgraph with just the seed
       human message ("Human: <ticker>") so each parallel branch starts
       with an empty conversation — no cross-contamination from the
       parent's messages or from sibling analyst branches.
    2. Invokes the subgraph to completion (analyst ⇄ tools loop).
    3. Returns ONLY the report field to the parent. Without this filter,
       the subgraph's internal ``messages`` would be appended to the
       parent's ``messages`` channel (via the ``add_messages`` reducer)
       and parallel siblings would still trample each other.
    """
    def node(state):
        sub_input = {
            "messages": [("human", state["company_of_interest"])],
            "company_of_interest": state["company_of_interest"],
            "trade_date": state["trade_date"],
            "past_context": state.get("past_context", ""),
        }
        result = subgraph.invoke(sub_input)
        return {report_field: result.get(report_field, "")}
    return node
