"""Native OpenAI Agents SDK capture (its tracing is not OpenTelemetry).

    import agents, evalops.traces as et
    agents.add_trace_processor(et.openai_agents_processor())   # once, at startup
    result = await agents.Runner.run(agent, question, max_turns=10)   # max_turns = hard stop

Span types mapped: agent, function (tool), generation / response (llm with usage), handoff,
guardrail, custom. The SDK swallows processor exceptions, so the circuit breaker cannot abort a run
from here: rely on max_turns plus eval.row_limits.max_seconds (async timeout). VERIFY span_data fields
against your SDK version.
"""

from __future__ import annotations

import datetime as dt
import threading

from .capture import _row
from .model import Span

try:  # optional dependency
    from agents.tracing import TracingProcessor as _Base
except Exception:  # noqa: BLE001
    _Base = object

KIND = {
    "agent": "agent",
    "function": "tool",
    "generation": "llm",
    "response": "llm",
    "handoff": "handoff",
    "guardrail": "guardrail",
    "custom": "other",
    "mcp_tools": "tool",
}


def _ns(ts) -> int:
    if not ts:
        return 0
    try:
        return int(dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return 0


def _usage(data) -> tuple[int, int]:
    u = getattr(data, "usage", None)
    if u is None and getattr(data, "response", None) is not None:
        u = getattr(data.response, "usage", None)
    if u is None:
        return 0, 0
    get = u.get if isinstance(u, dict) else (lambda k, d=0: getattr(u, k, d))
    return int(get("input_tokens", 0) or 0), int(get("output_tokens", 0) or 0)


class EvalopsAgentsProcessor(_Base):
    def __init__(self):
        self._traces: dict[str, object] = {}
        self._lock = threading.Lock()

    def _cap(self, trace_id):
        with self._lock:
            return self._traces.get(trace_id) or _row.get()

    def on_trace_start(self, trace):
        cap = _row.get()
        if cap is not None:
            with self._lock:
                self._traces[trace.trace_id] = cap

    def on_trace_end(self, trace):
        with self._lock:
            self._traces.pop(trace.trace_id, None)

    def on_span_start(self, span):
        cap = self._cap(span.trace_id)
        if cap is not None and getattr(span.span_data, "type", "") in ("function", "generation", "response"):
            cap.started_steps += 1
            cap._update_limits()

    def on_span_end(self, span):
        cap = self._cap(span.trace_id)
        if cap is None:
            return
        data = span.span_data
        typ = getattr(data, "type", "custom")
        s = Span(
            span_id=str(span.span_id),
            parent_id=getattr(span, "parent_id", None),
            name=getattr(data, "name", None) or typ,
            kind=KIND.get(typ, "other"),
            start_ns=_ns(getattr(span, "started_at", None)),
            end_ns=_ns(getattr(span, "ended_at", None)),
        )
        if typ == "agent":
            s.agent_name = data.name
        elif typ == "function":
            s.tool_name, s.inputs, s.outputs = data.name, getattr(data, "input", None), getattr(data, "output", None)
        elif typ in ("generation", "response"):
            s.model = getattr(data, "model", None)
            s.input_tokens, s.output_tokens = _usage(data)
        elif typ == "handoff":
            s.inputs, s.outputs = getattr(data, "from_agent", None), getattr(data, "to_agent", None)
            s.name = f"handoff {s.inputs} -> {s.outputs}"
        err = getattr(span, "error", None)
        if err:
            s.error = err.get("message") if isinstance(err, dict) else str(err)
        cap.add(s, "openai_agents")

    def shutdown(self):
        return None

    def force_flush(self):
        return None


_SINGLETON = EvalopsAgentsProcessor()


def openai_agents_processor() -> EvalopsAgentsProcessor:
    return _SINGLETON
