"""Map OpenTelemetry spans from any agent framework into the normalized Span model.

Dialects handled (detected per span, first match wins):
  1. OpenTelemetry GenAI semantic conventions  (gen_ai.operation.name: chat, execute_tool, invoke_agent, ...)
     -> Google ADK, Strands, Pydantic AI, Semantic Kernel, AutoGen, OpenLIT, Logfire, Datadog-style
  2. OpenInference                             (openinference.span.kind: LLM, TOOL, AGENT, CHAIN, RETRIEVER, ...)
     -> Arize Phoenix instrumentors: LangChain/LangGraph, LlamaIndex, CrewAI, OpenAI Agents, smolagents, DSPy, ADK...
  3. OpenLLMetry / Traceloop                   (traceloop.span.kind: workflow, task, agent, tool)
  4. Name heuristics                           (e.g. ADK 'call_llm', Pydantic AI 'running tool')
The GenAI conventions are still "Development" status and moved to their own repository in June 2026,
so attribute names may change: keep all mapping here (VERIFY against your instrumentation versions).
"""

from __future__ import annotations

import json
from typing import Any

from .model import Span

GENAI_OP = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "completion": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "invoke_workflow": "chain",
    "retrieval": "retrieval",
    "plan": "chain",
}
OPENINFERENCE = {
    "LLM": "llm",
    "TOOL": "tool",
    "AGENT": "agent",
    "CHAIN": "chain",
    "RETRIEVER": "retrieval",
    "RERANKER": "retrieval",
    "EMBEDDING": "embedding",
    "GUARDRAIL": "guardrail",
}
TRACELOOP = {"workflow": "chain", "task": "chain", "agent": "agent", "tool": "tool"}
NAME_PREFIX = [
    ("execute_tool", "tool"),
    ("invoke_agent", "agent"),
    ("create_agent", "agent"),
    ("call_llm", "llm"),
    ("running tool", "tool"),
    ("agent run", "agent"),
    ("chat ", "llm"),
    ("invoke_workflow", "chain"),
]

KEEP_ATTRS = (
    "gen_ai.operation.name",
    "openinference.span.kind",
    "traceloop.span.kind",
    "gen_ai.system",
    "gen_ai.provider.name",
    "gen_ai.tool.call.id",
    "gen_ai.agent.id",
    "langgraph_node",
)

#: Every raw attribute name this module consumes (all dialect aliases) plus the ones it keeps in
#: `Span.attributes`. Anything else is reported by *name* in `Span.unknown_attributes`, never by value.
KNOWN_ATTRS = frozenset(
    {
        *KEEP_ATTRS,
        # classification
        "llm.request.type",
        # models
        "gen_ai.request.model",
        "gen_ai.response.model",
        "llm.model_name",
        "llm.request.model",
        # token usage
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.prompt_tokens",
        "llm.token_count.prompt",
        "llm.usage.prompt_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.completion_tokens",
        "llm.token_count.completion",
        "llm.usage.completion_tokens",
        # generic payloads
        "input.value",
        "traceloop.entity.input",
        "output.value",
        "traceloop.entity.output",
        # tool name / arguments / result
        "gen_ai.tool.name",
        "tool.name",
        "traceloop.entity.name",
        "gen_ai.tool.call.arguments",
        "gcp.vertex.agent.tool_call_args",
        "tool_arguments",
        "tool.parameters",
        "gen_ai.tool.call.result",
        "gcp.vertex.agent.tool_response",
        "tool_response",
        # agent names and LangChain/OpenInference metadata (holds `langgraph_node`)
        "gen_ai.agent.name",
        "agent_name",
        "agent.name",
        "metadata",
        # LLM messages
        "gen_ai.input.messages",
        "gcp.vertex.agent.llm_request",
        "gen_ai.output.messages",
        "gcp.vertex.agent.llm_response",
        # errors
        "error.type",
    }
)


def _first(attrs: dict, *keys, default=None):
    for k in keys:
        v = attrs.get(k)
        if v not in (None, ""):
            return v
    return default


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def classify(name: str, attrs: dict) -> tuple[str, str | None]:
    op = attrs.get("gen_ai.operation.name")
    if op in GENAI_OP:
        return GENAI_OP[op], op
    oi = str(attrs.get("openinference.span.kind", "")).upper()
    if oi in OPENINFERENCE:
        return OPENINFERENCE[oi], None
    tl = str(attrs.get("traceloop.span.kind", "")).lower()
    if tl in TRACELOOP:
        return TRACELOOP[tl], None
    if attrs.get("llm.request.type") or attrs.get("gen_ai.request.model"):
        return "llm", None
    low = name.lower()
    for prefix, kind in NAME_PREFIX:
        if low.startswith(prefix):
            return kind, prefix.strip().replace(" ", "_")
    return "other", None


def _suffix(name: str, prefix: str) -> str | None:
    if not name.lower().startswith(prefix):
        return None
    return name[len(prefix) :].strip() or None


def normalize(raw: dict, seq: int = 0) -> Span:
    """raw: {span_id, parent_id, name, start_ns, end_ns, attributes, error} (see from_otel_span)."""
    name, attrs = raw.get("name") or "span", dict(raw.get("attributes") or {})
    meta = _maybe_json(attrs.get("metadata"))  # OpenInference LangChain puts langgraph_node here
    if isinstance(meta, dict) and meta.get("langgraph_node"):
        attrs.setdefault("langgraph_node", meta["langgraph_node"])
    kind, op = classify(name, attrs)

    span = Span(
        span_id=str(raw.get("span_id")),
        parent_id=raw.get("parent_id"),
        name=name,
        kind=kind,
        start_ns=_int(raw.get("start_ns")),
        end_ns=_int(raw.get("end_ns")),
        error=raw.get("error"),
        seq=seq,
    )
    span.attributes = {k: attrs[k] for k in KEEP_ATTRS if k in attrs}
    if op:
        span.attributes["op"] = op
    span.model = _first(attrs, "gen_ai.request.model", "gen_ai.response.model", "llm.model_name", "llm.request.model")
    span.input_tokens = _int(
        _first(
            attrs,
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.prompt_tokens",
            "llm.token_count.prompt",
            "llm.usage.prompt_tokens",
        )
    )
    span.output_tokens = _int(
        _first(
            attrs,
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.completion_tokens",
            "llm.token_count.completion",
            "llm.usage.completion_tokens",
        )
    )

    generic_in = _first(attrs, "input.value", "traceloop.entity.input")
    generic_out = _first(attrs, "output.value", "traceloop.entity.output")
    if kind == "tool":
        span.tool_name = (
            _first(attrs, "gen_ai.tool.name", "tool.name", "traceloop.entity.name")
            or _suffix(name, "execute_tool")
            or _suffix(name, "running tool")
            or name
        )
        span.inputs = _maybe_json(
            _first(
                attrs,
                "gen_ai.tool.call.arguments",
                "gcp.vertex.agent.tool_call_args",
                "tool_arguments",
                "tool.parameters",
                default=generic_in,
            )
        )
        span.outputs = _maybe_json(
            _first(
                attrs, "gen_ai.tool.call.result", "gcp.vertex.agent.tool_response", "tool_response", default=generic_out
            )
        )
        if isinstance(span.inputs, dict) and set(span.inputs) == {"input"} and isinstance(span.inputs["input"], dict):
            span.inputs = span.inputs["input"]
    elif kind == "agent":
        span.agent_name = (
            _first(attrs, "gen_ai.agent.name", "agent_name", "agent.name", "langgraph_node")
            or _suffix(name, "invoke_agent")
            or _suffix(name, "create_agent")
            or name
        )
        span.inputs, span.outputs = _maybe_json(generic_in), _maybe_json(generic_out)
    elif kind == "llm":
        span.inputs = _maybe_json(
            _first(attrs, "gen_ai.input.messages", "gcp.vertex.agent.llm_request", default=generic_in)
        )
        span.outputs = _maybe_json(
            _first(attrs, "gen_ai.output.messages", "gcp.vertex.agent.llm_response", default=generic_out)
        )
    else:
        if attrs.get("langgraph_node") and name == attrs["langgraph_node"]:
            span.kind, span.agent_name = "agent", name  # LangGraph node -> route step
        span.inputs, span.outputs = _maybe_json(generic_in), _maybe_json(generic_out)
    if not span.error and attrs.get("error.type"):
        span.error = str(attrs["error.type"])
    # Report unmodeled attributes by name only, sorted and deduplicated: a raw value may be a secret.
    span.unknown_attributes = sorted({str(k) for k in attrs if k not in KNOWN_ATTRS})
    return span


def from_otel_span(span) -> tuple[str, dict]:
    """OpenTelemetry SDK ReadableSpan (duck-typed) -> (trace_id_hex, raw dict)."""
    ctx = span.get_span_context() if hasattr(span, "get_span_context") else span.context
    parent = getattr(span, "parent", None)
    status = getattr(span, "status", None)
    error = None
    if status is not None and str(getattr(status, "status_code", "")).upper().endswith("ERROR"):
        error = getattr(status, "description", None) or "error"
        for ev in getattr(span, "events", None) or []:
            if getattr(ev, "name", "") == "exception":
                error = (getattr(ev, "attributes", {}) or {}).get("exception.message", error)
    raw = {
        "span_id": format(ctx.span_id, "016x"),
        "parent_id": format(parent.span_id, "016x") if parent else None,
        "name": span.name,
        "start_ns": getattr(span, "start_time", 0) or 0,
        "end_ns": getattr(span, "end_time", 0) or 0,
        "attributes": dict(getattr(span, "attributes", {}) or {}),
        "error": error,
    }
    return format(ctx.trace_id, "032x"), raw
