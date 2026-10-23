"""In-process trace capture during evals, plus the per-row circuit breaker.

Each eval row runs inside `row_capture(...)`. Spans from any capture path land in that row's buffer:
  * OpenTelemetry: a SpanProcessor attached to the global SDK TracerProvider (your exporters to
    Langfuse/Logfire/Phoenix/MLflow/Datadog keep working: evalops only adds a processor);
  * LangChain/LangGraph callbacks: evalops.traces.langchain_callbacks();
  * OpenAI Agents SDK: evalops.traces.openai_agents_processor().
No backend round-trip is needed, so there is no ingestion delay and scoring is backend-independent.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time

from .model import AgentTrace, Span
from .normalize import from_otel_span, normalize

_row: contextvars.ContextVar = contextvars.ContextVar("evalops_row_capture", default=None)
_by_trace: dict[str, "RowCapture"] = {}
_by_span: dict[str, "RowCapture"] = {}
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Raised (where the framework allows) when a row exceeds its step/token/time limits."""


class RowCapture:
    def __init__(self, row_id: str, limits: dict | None = None):
        self.row_id = row_id
        self.limits = {k: v for k, v in (limits or {}).items() if v}
        self.spans: list[Span] = []
        self.sources: set[str] = set()
        self.trace_id: str | None = None
        self.started_steps = 0
        self.exceeded: str | None = None
        self.t0 = time.monotonic()
        self._lock = threading.Lock()
        self._seq = 0
        self.finalizers: list = []  # adapters close still-open spans (e.g. after an abort)

    # ---- recording ----
    def add(self, span: Span, source: str) -> None:
        if span.name == "evalops.row":
            return
        with self._lock:
            self._seq += 1
            span.seq = self._seq
            self.spans.append(span)
            self.sources.add(source)
        self._update_limits()

    def step_started(self) -> None:
        """Called by native adapters before an LLM/tool call; raises if the budget is already spent."""
        with self._lock:
            self.started_steps += 1
        self._update_limits(pending=True)
        self.check()

    def _update_limits(self, pending: bool = False) -> None:
        if self.exceeded:
            return
        steps = max(self.started_steps, sum(1 for s in self.spans if s.kind in ("llm", "tool")))
        tokens = sum(s.input_tokens + s.output_tokens for s in self.spans if s.kind == "llm")
        secs = time.monotonic() - self.t0
        lim = self.limits
        if lim.get("max_steps") and steps > lim["max_steps"]:
            self.exceeded = f"max_steps ({steps} > {lim['max_steps']})"
        elif lim.get("max_tokens") and tokens > lim["max_tokens"]:
            self.exceeded = f"max_tokens ({tokens} > {lim['max_tokens']})"
        elif lim.get("max_seconds") and secs > lim["max_seconds"]:
            self.exceeded = f"max_seconds ({secs:.1f} > {lim['max_seconds']})"

    def check(self) -> None:
        if self.exceeded:
            raise BudgetExceeded(f"row {self.row_id} exceeded {self.exceeded}")

    def trace(self) -> AgentTrace | None:
        if not self.spans:
            return None
        source = next(iter(self.sources)) if len(self.sources) == 1 else "mixed"
        return AgentTrace(spans=list(self.spans), trace_id=self.trace_id, source=source)


def current() -> RowCapture | None:
    return _row.get()


def check_budget() -> None:
    """Cooperative circuit breaker: call inside long agent loops / custom frameworks."""
    cap = _row.get()
    if cap:
        cap._update_limits()
        cap.check()


# ---------------- OpenTelemetry ----------------
try:  # optional dependency
    from opentelemetry.sdk.trace import SpanProcessor as _OtelBase
except Exception:  # noqa: BLE001
    _OtelBase = object


class CaptureProcessor(_OtelBase):
    """SpanProcessor routing finished spans to the RowCapture of the eval row that produced them."""

    def on_start(self, span, parent_context=None):
        cap = _row.get()
        try:
            ctx = span.get_span_context()
            tid, sid = format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
        except Exception:  # noqa: BLE001
            return
        cap = _by_trace.get(tid) or cap
        if cap is not None:
            with _lock:
                _by_span[sid] = cap

    def on_end(self, span):
        try:
            tid, raw = from_otel_span(span)
        except Exception:  # noqa: BLE001
            return
        with _lock:
            cap = _by_span.pop(raw["span_id"], None) or _by_trace.get(tid)
        if cap is not None:
            cap.add(normalize(raw), "otel")

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis: int = 30000):
        return True


_PROCESSOR = CaptureProcessor()
_installed = False


def install_otel() -> bool:
    """Attach the capture processor to the global SDK TracerProvider (creating one if needed)."""
    global _installed
    if _installed:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
    except Exception:  # noqa: BLE001
        return False
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(_PROCESSOR)
        _installed = True
    return _installed


@contextlib.contextmanager
def row_capture(row_id: str, limits: dict | None = None, attributes: dict | None = None):
    """Capture everything one eval row does. Opens an OTel root span when OTel is installed."""
    cap = RowCapture(row_id, limits)
    token = _row.set(cap)
    root = contextlib.nullcontext()
    if _installed:
        from opentelemetry import trace
        root = trace.get_tracer("evalops").start_as_current_span(
            "evalops.row", attributes={"evalops.row_id": row_id, **(attributes or {})})
    try:
        with root as span:
            if span is not None:
                cap.trace_id = format(span.get_span_context().trace_id, "032x")
                with _lock:
                    _by_trace[cap.trace_id] = cap
            yield cap
    finally:
        for fin in cap.finalizers:
            try:
                fin(cap.exceeded)
            except Exception:  # noqa: BLE001
                pass
        _row.reset(token)
        if cap.trace_id:
            with _lock:
                _by_trace.pop(cap.trace_id, None)
