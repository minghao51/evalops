"""Normalized, framework-neutral agent trace. Scorers only ever see this model.

Every capture path (OpenTelemetry GenAI conventions, OpenInference, OpenLLMetry, Google ADK,
Pydantic AI, LangChain/LangGraph callbacks, OpenAI Agents SDK processors) maps into it, so the
same trajectory scorer works for every framework, offline in CI and online on production traces.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..contract import redact
from ..util import FormatError

KINDS = ("agent", "llm", "tool", "retrieval", "embedding", "chain", "guardrail", "handoff", "other")

#: The nested trace document's own version (CONTRACT §5.6), independent of `FORMAT_VERSION`.
#: Writers always emit this value; readers accept an absent version as v1 and an explicit 1 or 2.
TRACE_SCHEMA_VERSION = 2
_READABLE_TRACE_VERSIONS = (1, TRACE_SCHEMA_VERSION)

#: The closed set of capture-path labels `AgentTrace.source` may carry.
SOURCE = ("otel", "langchain", "openai_agents", "mixed", "stored", "unknown")


@dataclass
class Span:
    span_id: str
    name: str
    kind: str = "other"
    parent_id: str | None = None
    start_ns: int = 0
    end_ns: int = 0
    inputs: Any = None
    outputs: Any = None
    model: str | None = None
    tool_name: str | None = None
    agent_name: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    attributes: dict = field(default_factory=dict)  # small, filtered raw attributes (e.g. operation)
    seq: int = 0  # capture order, tie-breaker for equal timestamps
    # Names (never values: a value may be a secret) of raw attributes `normalize()` did not model.
    unknown_attributes: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0, self.end_ns - self.start_ns) / 1e9


@dataclass
class AgentTrace:
    spans: list[Span]
    trace_id: str | None = None
    source: str = "unknown"  # one of SOURCE
    schema_version: int = TRACE_SCHEMA_VERSION  # the version this document was *read* with

    # ---------- structure ----------
    def ordered(self) -> list[Span]:
        return sorted(self.spans, key=lambda s: (s.start_ns, s.seq))

    def of_kind(self, kind: str) -> list[Span]:
        return [s for s in self.ordered() if s.kind == kind]

    def children(self, span_id: str) -> list[Span]:
        return [s for s in self.ordered() if s.parent_id == span_id]

    def roots(self) -> list[Span]:
        ids = {s.span_id for s in self.spans}
        return [s for s in self.ordered() if s.parent_id not in ids]

    # ---------- trajectory views ----------
    def tool_calls(self) -> list[Span]:
        return self.of_kind("tool")

    def tool_names(self) -> list[str]:
        return [s.tool_name or s.name for s in self.tool_calls()]

    def llm_calls(self) -> list[Span]:
        return self.of_kind("llm")

    def agent_route(self) -> list[str]:
        """Agents/sub-agents/graph nodes in execution order, consecutive duplicates collapsed."""
        route: list[str] = []
        for s in self.of_kind("agent"):
            if s.attributes.get("op") == "create_agent":
                continue
            name = s.agent_name or s.name
            if not route or route[-1] != name:
                route.append(name)
        return route

    def handoffs(self) -> list[Span]:
        return self.of_kind("handoff")

    def errors(self) -> list[Span]:
        return [s for s in self.ordered() if s.error]

    @property
    def steps(self) -> int:
        return len(self.llm_calls()) + len(self.tool_calls())

    def tokens(self) -> tuple[int, int]:
        """(input, output) from LLM spans; falls back to the top-most spans that carry usage."""
        llm = self.llm_calls()
        tin, tout = sum(s.input_tokens for s in llm), sum(s.output_tokens for s in llm)
        if tin or tout:
            return tin, tout
        carrying = [s for s in self.spans if s.input_tokens or s.output_tokens]
        ids = {s.span_id for s in carrying}
        top = [s for s in carrying if s.parent_id not in ids]
        return sum(s.input_tokens for s in top), sum(s.output_tokens for s in top)

    @property
    def duration_s(self) -> float:
        if not self.spans:
            return 0.0
        return max(0, max(s.end_ns for s in self.spans) - min(s.start_ns for s in self.spans)) / 1e9

    def unknown_attributes(self) -> dict[str, int]:
        """`{attribute name: number of spans carrying it}` over the whole trace (sorted, names only)."""
        counts: dict[str, int] = {}
        for span in self.spans:
            for name in span.unknown_attributes:
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items()))

    def metrics(self) -> dict[str, float]:
        tools = self.tool_calls()
        tin, tout = self.tokens()
        return {
            "steps": self.steps,
            "llm_calls": len(self.llm_calls()),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for s in tools if s.error),
            "agents": len(self.agent_route()),
            "handoffs": len(self.handoffs()),
            "errors": len(self.errors()),
            "latency_s": round(self.duration_s, 4),
            "input_tokens": tin,
            "output_tokens": tout,
        }

    # ---------- serialization / display ----------
    def to_dict(self, max_payload_chars: int | None = 4000) -> dict:
        def clip(v):
            if max_payload_chars is None or v is None:
                return v
            text = v if isinstance(v, str) else json.dumps(v, default=str)
            return v if len(text) <= max_payload_chars else text[:max_payload_chars] + "…[truncated]"

        spans = []
        for s in self.spans:
            d = asdict(s)
            d["inputs"], d["outputs"] = clip(s.inputs), clip(s.outputs)
            spans.append(d)
        # Writers always emit the current version: a trace read as v1 (or unversioned) is upgraded
        # on write. Unknown top-level/span keys were already dropped by `from_dict`.
        document = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "source": self.source,
            "spans": spans,
        }
        # Outbound boundary (CONTRACT §5.1): the persisted/emitted trace is a redacted *deep copy*.
        # The in-memory trace -- what scorers, `render` and replay see -- is never modified.
        return redact.redact_document(document)

    @classmethod
    def from_dict(cls, d: dict | None, source: str | None = None) -> AgentTrace | None:
        """Read a trace document; `source` names the *artifact* in a format error (exit 7).

        An absent `schema_version` is v1 (the legacy shape); explicit 1 and 2 are accepted and the
        returned object records the version it read. Anything else is a contract mismatch. Unknown
        top-level and unknown span keys are ignored, never a `TypeError`.
        """
        if not d:
            return None
        found = d.get("schema_version", 1)
        if isinstance(found, bool) or not isinstance(found, int) or found not in _READABLE_TRACE_VERSIONS:
            raise FormatError(source or "trace", found, TRACE_SCHEMA_VERSION)
        known = {f.name for f in fields(Span)}
        spans = [Span(**{k: v for k, v in s.items() if k in known}) for s in (d.get("spans") or [])]
        return cls(
            spans=spans,
            trace_id=d.get("trace_id"),
            source=d.get("source") or "stored",
            schema_version=found,
        )

    def render(self, max_chars: int = 200) -> str:
        """Compact indented tree, e.g. for an LLM trajectory judge or `evalops traces`."""
        lines = []

        def short(v):
            if v is None:
                return ""
            t = v if isinstance(v, str) else json.dumps(v, default=str)
            return t if len(t) <= max_chars else t[:max_chars] + "…"

        def walk(span, depth):
            label = span.tool_name or span.agent_name or span.model or span.name
            extra = []
            if span.kind == "tool":
                extra.append(f"args={short(span.inputs)} -> {short(span.outputs)}")
            if span.input_tokens or span.output_tokens:
                extra.append(f"tok={span.input_tokens}/{span.output_tokens}")
            if span.error:
                extra.append(f"ERROR={short(span.error)}")
            lines.append(f"{'  ' * depth}[{span.kind}] {label} ({span.duration_s:.2f}s) {' '.join(extra)}".rstrip())
            for c in self.children(span.span_id):
                walk(c, depth + 1)

        for r in self.roots():
            walk(r, 0)
        return "\n".join(lines)
