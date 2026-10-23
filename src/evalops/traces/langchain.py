"""Native LangChain / LangGraph capture (no OpenTelemetry needed).

    from evalops.traces import langchain_callbacks
    graph.invoke(inputs, config={"callbacks": langchain_callbacks(), "recursion_limit": 25})

* LangGraph nodes become `agent` spans (so supervisor/sub-agent routes are scoreable), tools become
  `tool` spans, chat models become `llm` spans with token usage.
* The circuit breaker is ENFORCED here: on_tool_start / on_llm_start raise BudgetExceeded once the
  row's step/token/time budget is spent (handler.raise_error = True), aborting the graph run.
* Outside an evalops row, langchain_callbacks() returns [] so production code is unaffected.
Use either this OR an OTel instrumentor for LangChain (OpenInference), not both, to avoid duplicates.
"""
from __future__ import annotations

import time
from typing import Any

from .capture import BudgetExceeded, current
from .model import Span

try:  # optional dependency
    from langchain_core.callbacks import BaseCallbackHandler as _Base
except Exception:  # noqa: BLE001
    _Base = object


def _content(x: Any) -> Any:
    if hasattr(x, "content"):
        return x.content
    if isinstance(x, list):
        return [_content(i) for i in x]
    return x


def _usage_from_result(response) -> tuple[int, int]:
    llm_output = getattr(response, "llm_output", None) or {}
    tu = llm_output.get("token_usage") or llm_output.get("usage") or {}
    tin = tu.get("prompt_tokens") or tu.get("input_tokens") or 0
    tout = tu.get("completion_tokens") or tu.get("output_tokens") or 0
    if tin or tout:
        return int(tin), int(tout)
    for gens in getattr(response, "generations", None) or []:
        for g in gens:
            um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
            tin += int(um.get("input_tokens", 0) or 0)
            tout += int(um.get("output_tokens", 0) or 0)
    return tin, tout


class EvalopsCallbackHandler(_Base):
    raise_error = True      # let BudgetExceeded abort the run
    run_inline = True

    def __init__(self, capture, nodes_as_agents: bool = True):
        if _Base is not object:
            super().__init__()
        self.cap = capture
        self.nodes_as_agents = nodes_as_agents
        self.open: dict[str, Span] = {}
        capture.finalizers.append(self._close_open)

    def _close_open(self, reason=None):
        """Spans still open when the row ends (abort, crash): record them so the tree stays intact."""
        for run_id in list(self.open):
            self._end(run_id, error=RuntimeError(f"unfinished: {reason or 'run aborted'}"))

    # ---- helpers ----
    def _start(self, run_id, parent_run_id, name, kind, inputs=None, **fields):
        s = Span(span_id=str(run_id), parent_id=str(parent_run_id) if parent_run_id else None, name=name or kind,
                 kind=kind, start_ns=time.time_ns(), inputs=inputs, **fields)
        self.open[str(run_id)] = s
        return s

    def _end(self, run_id, outputs=None, error=None):
        s = self.open.pop(str(run_id), None)
        if s is None:
            return None
        s.end_ns, s.outputs = time.time_ns(), outputs
        if error is not None:
            s.error = f"{type(error).__name__}: {error}"
        self.cap.add(s, "langchain")
        return s

    # ---- chains / LangGraph nodes ----
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kw):
        name = kw.get("name") or (serialized or {}).get("name") or ((serialized or {}).get("id") or ["chain"])[-1]
        node = (metadata or {}).get("langgraph_node")
        is_node = self.nodes_as_agents and node and name == node
        self._start(run_id, parent_run_id, name, "agent" if is_node else "chain", inputs=inputs,
                    agent_name=node if is_node else None,
                    attributes={"langgraph_node": node} if node else {})

    def on_chain_end(self, outputs, *, run_id, **kw):
        self._end(run_id, outputs)

    def on_chain_error(self, error, *, run_id, **kw):
        self._end(run_id, error=error)

    # ---- tools ----
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kw):
        self.cap.step_started()  # may raise BudgetExceeded -> aborts before the tool runs
        name = kw.get("name") or (serialized or {}).get("name") or "tool"
        self._start(run_id, parent_run_id, name, "tool", inputs=inputs if inputs is not None else input_str,
                    tool_name=name)

    def on_tool_end(self, output, *, run_id, **kw):
        self._end(run_id, _content(output))

    def on_tool_error(self, error, *, run_id, **kw):
        if isinstance(error, BudgetExceeded):
            self.open.pop(str(run_id), None)
            return
        self._end(run_id, error=error)

    # ---- models ----
    def _llm_start(self, serialized, payload, run_id, parent_run_id, kw):
        self.cap.step_started()
        params = kw.get("invocation_params") or {}
        meta = kw.get("metadata") or {}
        model = params.get("model") or params.get("model_name") or meta.get("ls_model_name")
        name = kw.get("name") or (serialized or {}).get("name") or "llm"
        self._start(run_id, parent_run_id, name, "llm", inputs=payload, model=model)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
        self._llm_start(serialized, _content(messages), run_id, parent_run_id, kw)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw):
        self._llm_start(serialized, prompts, run_id, parent_run_id, kw)

    def on_llm_end(self, response, *, run_id, **kw):
        s = self.open.get(str(run_id))
        if s is not None:
            s.input_tokens, s.output_tokens = _usage_from_result(response)
        texts = [getattr(g, "text", None) for gens in (getattr(response, "generations", None) or []) for g in gens]
        self._end(run_id, texts[0] if len(texts) == 1 else texts)

    def on_llm_error(self, error, *, run_id, **kw):
        self._end(run_id, error=error)

    # ---- retrievers ----
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, **kw):
        self._start(run_id, parent_run_id, kw.get("name") or "retriever", "retrieval", inputs=query)

    def on_retriever_end(self, documents, *, run_id, **kw):
        self._end(run_id, [getattr(d, "page_content", str(d)) for d in documents])

    def on_retriever_error(self, error, *, run_id, **kw):
        self._end(run_id, error=error)


def langchain_callbacks(nodes_as_agents: bool = True) -> list:
    cap = current()
    return [EvalopsCallbackHandler(cap, nodes_as_agents)] if cap is not None else []
