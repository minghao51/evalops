"""A tiny multi-agent "graph" that emits LangChain-style callbacks exactly like LangGraph does
(on_chain_start with metadata.langgraph_node, on_chat_model_start/on_llm_end with usage_metadata,
on_tool_start/on_tool_end), so the LangGraph capture path is exercised without installing langchain.

Prompt flags (in prompts/router.txt) inject realistic agent bugs:
  ROUTE_FLAKY  -> every other run routes refund questions to the wrong sub-agent (non-determinism)
  LOOP         -> the agent keeps calling the same tool (runaway loop -> circuit breaker)
"""
import itertools
import os
import uuid

import evalops
from evalops.scorers import (agent_route, expected_state, forbidden_tools, no_loops, step_budget, tool_args,
                             tool_precision, tool_trajectory)
from evalops.traces import langchain_callbacks

_flaky = itertools.count()
LIVE_CALLS = {"n": 0}


# ---------------- tools (record/replay) ----------------
@evalops.replayable("lookup_order")
def lookup_order(order_id: str) -> dict:
    if os.getenv("TOOLS_OFFLINE") == "1":
        raise RuntimeError("live tool called while offline")
    LIVE_CALLS["n"] += 1
    return {"order_id": order_id, "status": "delivered", "total": 89.9}


@evalops.replayable("issue_refund")
def issue_refund(order_id: str, amount: float) -> dict:
    if os.getenv("TOOLS_OFFLINE") == "1":
        raise RuntimeError("live tool called while offline")
    return {"order_id": order_id, "refunded": amount, "status": "refunded"}


@evalops.replayable("policy_search")
def policy_search(query: str) -> str:
    if os.getenv("TOOLS_OFFLINE") == "1":
        raise RuntimeError("live tool called while offline")
    return "Refunds within 30 days. Hours 9am-5pm Monday-Friday."


# ---------------- fake LangChain objects ----------------
class _Msg:
    def __init__(self, content, tin, tout):
        self.content = content
        self.usage_metadata = {"input_tokens": tin, "output_tokens": tout}


class _Gen:
    def __init__(self, text, tin, tout):
        self.text, self.message = text, _Msg(text, tin, tout)


class _Result:
    def __init__(self, text, tin, tout):
        self.generations, self.llm_output = [[_Gen(text, tin, tout)]], {}


class FakeGraph:
    def __init__(self, router_prompt: str, callbacks):
        self.prompt, self.cbs = router_prompt, callbacks

    def _emit(self, method, *a, **kw):
        for cb in self.cbs:
            getattr(cb, method)(*a, **kw)

    def _llm(self, parent, text, model="fake-gpt"):
        rid = uuid.uuid4()
        self._emit("on_chat_model_start", {"name": "ChatFake"}, [[text]], run_id=rid, parent_run_id=parent,
                   invocation_params={"model": model})
        self._emit("on_llm_end", _Result("ok", 120, 30), run_id=rid)

    def _tool(self, parent, name, fn, **args):
        rid = uuid.uuid4()
        self._emit("on_tool_start", {"name": name}, str(args), run_id=rid, parent_run_id=parent, inputs=args)
        try:
            out = fn(**args)
        except Exception as e:
            self._emit("on_tool_error", e, run_id=rid)
            raise
        self._emit("on_tool_end", out, run_id=rid)
        return out

    def _node(self, parent, node):
        rid = uuid.uuid4()
        self._emit("on_chain_start", {"name": node}, {}, run_id=rid, parent_run_id=parent,
                   metadata={"langgraph_node": node}, name=node)
        return rid

    def _end(self, rid, out=None):
        self._emit("on_chain_end", out or {}, run_id=rid)

    def invoke(self, question: str) -> dict:
        root = uuid.uuid4()
        self._emit("on_chain_start", {"name": "LangGraph"}, {"question": question}, run_id=root, name="LangGraph")
        sup = self._node(root, "supervisor")
        self._llm(sup, self.prompt.replace("{{question}}", question))
        self._end(sup)
        refund = "refund" in question.lower()
        if refund and "ROUTE_FLAKY" in self.prompt and next(_flaky) % 2 == 0:
            refund = False  # wrong route on every other call
        if refund:
            node = self._node(root, "refund_agent")
            order = self._tool(node, "lookup_order", lookup_order, order_id="A1")
            self._llm(node, "decide refund")
            loops = 6 if "LOOP" in self.prompt else 0
            for _ in range(loops):
                self._tool(node, "lookup_order", lookup_order, order_id="A1")
            res = self._tool(node, "issue_refund", issue_refund, order_id=order["order_id"], amount=order["total"])
            self._end(node)
            out = {"answer": "Refund issued.", "status": res["status"]}
        else:
            node = self._node(root, "info_agent")
            text = self._tool(node, "policy_search", policy_search, query=question)
            self._llm(node, "answer from policy")
            self._end(node)
            out = {"answer": text, "status": "answered"}
        self._end(root, out)
        return out


# ---------------- evalops hooks ----------------
def behavior_config():
    return {"llm_endpoint": "fake-gpt@1", "graph": "supervisor-v1"}


def build_predict_fn(prompts):
    router = prompts["router"]

    def predict(question: str) -> dict:
        graph = FakeGraph(router.template, langchain_callbacks())   # <- the one line LangGraph users add
        return graph.invoke(question)
    return predict


@evalops.scorer("helpful", llm=True)
def helpful(*, outputs):
    flip = os.getenv("JUDGE_BROKEN") == "1"
    ok = isinstance(outputs, dict) and outputs.get("status") in ("refunded", "answered")
    if isinstance(outputs, str):
        ok = "sorry" not in outputs.lower()
    return (not ok) if flip else ok


def build_scorers(evaluator="core"):
    return [helpful, tool_trajectory("in_order"), tool_precision(), tool_args(), forbidden_tools(),
            step_budget(), no_loops(max_identical=3), agent_route("exact"), expected_state()]


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 1}
