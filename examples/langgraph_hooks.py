"""Example: real LangGraph integration (requires langgraph + langchain-openai; not run in evalops' CI).

The only evalops-specific lines are marked  # <- evalops
"""
import os

import evalops
from evalops.scorers import agent_route, no_loops, step_budget, tool_args, tool_precision, tool_trajectory
from evalops.traces import langchain_callbacks
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


@tool
@evalops.replayable("lookup_order")                    # <- evalops: record/replay in CI
def lookup_order(order_id: str) -> dict:
    """Look up an order by id."""
    return {"order_id": order_id, "status": "delivered", "total": 89.9}   # your real API call


def behavior_config():
    return {"model": os.getenv("APP_MODEL", "gpt-4o-mini@2024-07-18"), "tools": ["lookup_order"],
            "graph": "react-v1"}


def build_predict_fn(prompts):
    system = prompts["agent_system"]          # prompts/agent_system.txt (no {{vars}})
    model = ChatOpenAI(model=behavior_config()["model"].split("@")[0], temperature=0)
    graph = create_react_agent(model, tools=[lookup_order], prompt=system.format())

    def predict(question: str) -> str:
        result = graph.invoke({"messages": [("user", question)]},
                              config={"callbacks": langchain_callbacks(),   # <- evalops: capture + breaker
                                      "recursion_limit": 20})
        return result["messages"][-1].content
    return predict


def build_scorers(evaluator="core"):
    return [tool_trajectory("in_order"), tool_precision(), tool_args(), step_budget(), no_loops(),
            agent_route(exclude=("tools",))]   # LangGraph ToolNode is named "tools"


def judge_meta():
    return {"judge_model": "none", "judge_prompt_version": "0", "llm_judge_count": 0}
