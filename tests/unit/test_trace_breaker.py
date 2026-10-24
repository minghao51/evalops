"""WP1.5 (C): circuit-breaker parity — one real regression test per capture path.

| path | breaker | the claim this module pins |
|---|---|---|
| `langchain` | `enforced` | `on_llm_start`/`on_tool_start` raise `BudgetExceeded` before the step |
| `otel` | `post-hoc` | the real `CaptureProcessor` never raises; a real eval row is `budget_exceeded` |
| `openai_agents` | `post-hoc` | the processor never raises; `cap.exceeded` is set on span start |
| `custom` | `cooperative` | `check_budget()` raises inside a row and is a no-op outside one |

No framework SDK is installed: the OTel/OpenAI objects are duck-typed span dumps, exactly like
`tests/unit/test_traces.py`. Every test is a zero-argument function, so the dependency-free runner
runs this module too.
"""

import pathlib
import shutil
import sys
import types
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops.traces import BudgetExceeded, check_budget, langchain_callbacks, openai_agents_processor, row_capture  # noqa: E402
from evalops.traces.capture import BREAKER_PARITY, CaptureProcessor  # noqa: E402
from evalops.traces.model import Span  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fake_otel_span(trace_id: int, span_id: int, name: str, attrs: dict, parent_id: int | None = None):
    ctx = types.SimpleNamespace(trace_id=trace_id, span_id=span_id)
    span = types.SimpleNamespace(
        name=name,
        attributes=dict(attrs),
        start_time=span_id,
        end_time=span_id + 1,
        events=[],
        parent=types.SimpleNamespace(span_id=parent_id) if parent_id else None,
        status=None,
    )
    span.get_span_context = lambda: ctx
    return span


def _traces(root, group: str) -> tuple[int, dict]:
    return proj.run_cli(root, "traces", "--group", group)


# ---------------------------------------------------------------------------
# (C1) the documented table
# ---------------------------------------------------------------------------
def test_c1_breaker_parity_names_the_four_capture_paths_and_their_honest_behaviour():
    assert BREAKER_PARITY == {
        "langchain": "enforced",
        "otel": "post-hoc",
        "openai_agents": "post-hoc",
        "custom": "cooperative",
    }


# ---------------------------------------------------------------------------
# (C3) LangChain: enforced
# ---------------------------------------------------------------------------
def test_c3_langchain_raises_budget_exceeded_before_the_next_llm_and_tool_call():
    with row_capture("row", {"max_steps": 1}) as cap:
        (handler,) = langchain_callbacks()  # the real capture entry point
        root, llm = uuid.uuid4(), uuid.uuid4()
        handler.on_chain_start({"name": "graph"}, {}, run_id=root, name="graph")
        handler.on_llm_start({"name": "ChatOpenAI"}, ["hi"], run_id=llm, parent_run_id=root)
        started = len(cap.spans)

        attempts = {
            "llm": lambda: handler.on_llm_start({}, ["again"], run_id=uuid.uuid4(), parent_run_id=root),
            "tool": lambda: handler.on_tool_start({"name": "search"}, "q", run_id=uuid.uuid4(), parent_run_id=root),
        }
        for kind, fire in attempts.items():
            try:
                fire()
            except BudgetExceeded as exc:
                assert "row exceeded max_steps" in str(exc), (kind, str(exc))
            else:  # pragma: no cover - only reached when the breaker is broken
                raise AssertionError(f"the langchain breaker did not abort the next {kind} call")
        assert len(cap.spans) == started  # no span was even started for the aborted steps
        handler.on_chain_end({}, run_id=root)


# ---------------------------------------------------------------------------
# (C3) OTel: post-hoc, through a real eval row
# ---------------------------------------------------------------------------
def test_c3_otel_capture_never_raises_and_a_real_eval_row_is_budget_exceeded():
    proc = CaptureProcessor()
    with row_capture("row", {"max_steps": 1}) as cap:
        for i in range(3):
            span = _fake_otel_span(7, 10 + i, "chat gpt-4o", {"gen_ai.operation.name": "chat"})
            proc.on_start(span)
            proc.on_end(span)  # observed, never aborted: no assertion-friendly raise can escape
    assert cap.exceeded is not None and cap.exceeded.startswith("max_steps (2 > 1)"), cap.exceeded
    assert len(cap.spans) == 3

    root = proj.project(
        config=OTEL_CONFIG,
        files={"trace_otel_app.py": OTEL_APP},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out  # the capture cannot abort a row: the command still succeeds
        metrics = out["data"]["runs"][0]["metrics"]
        assert metrics["budget_exceeded_rate"] == 1.0, metrics
        assert out["data"]["runs"][0]["errors"] == 1, out["data"]["runs"][0]
        code, traces = _traces(root, out["data"]["group"])
        assert code == 0, traces
        assert traces["data"]["rows"][0]["error"].startswith("budget_exceeded: max_steps"), traces["data"]["rows"][0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C3) OpenAI Agents SDK: post-hoc
# ---------------------------------------------------------------------------
def _agents_span(index: int, trace_id: str = "tr1") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        trace_id=trace_id,
        span_id=f"s{index}",
        parent_id=None,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        error=None,
        span_data=types.SimpleNamespace(type="generation", name=None, model="gpt-4o", usage=None, response=None),
    )


def test_c3_openai_agents_processor_sets_the_flag_on_span_start_and_never_raises():
    processor = openai_agents_processor()
    with row_capture("row", {"max_steps": 1}) as cap:
        processor.on_trace_start(types.SimpleNamespace(trace_id="tr1"))
        try:
            first = _agents_span(1)
            processor.on_span_start(first)
            processor.on_span_end(first)
            assert cap.exceeded is None  # one started step is within the limit

            second = _agents_span(2)
            processor.on_span_start(second)  # raises nothing: the SDK swallows processor exceptions
            # the flag is set by the *start* counter, before any span lands in the capture
            assert cap.exceeded is not None and cap.exceeded.startswith("max_steps (2 > 1)"), cap.exceeded
            processor.on_span_end(second)

            third = _agents_span(3)
            processor.on_span_start(third)
            processor.on_span_end(third)
        finally:
            processor.on_trace_end(types.SimpleNamespace(trace_id="tr1"))
    assert len(cap.spans) == 3  # every span was still recorded: post-hoc, not aborted


# ---------------------------------------------------------------------------
# (C3) custom loop: cooperative
# ---------------------------------------------------------------------------
def test_c3_custom_check_budget_raises_inside_a_row_and_is_a_no_op_outside_one():
    check_budget()  # outside an eval row: cooperative no-op
    with row_capture("row", {"max_steps": 1}) as cap:
        cap.add(Span(span_id="1", name="tool", kind="tool"), "custom")
        cap.add(Span(span_id="2", name="tool", kind="tool"), "custom")
        try:
            check_budget()
        except BudgetExceeded as exc:
            assert "row exceeded max_steps (2 > 1)" in str(exc), str(exc)
        else:  # pragma: no cover - only reached when the cooperative breaker is broken
            raise AssertionError("check_budget() did not raise inside an exhausted row")
    assert cap.exceeded is not None and cap.exceeded.startswith("max_steps")


# ---------------------------------------------------------------------------
# (C3) max_seconds: async only, and the row error is not relabelled
# ---------------------------------------------------------------------------
def test_c3_max_seconds_cuts_off_an_async_predict_and_keeps_the_timeout_error():
    root = proj.project(
        config=SLOW_CONFIG,
        files={"slow_app.py": SLOW_APP},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        metrics = out["data"]["runs"][0]["metrics"]
        # a timeout is NOT a budget breach: the row error names the real cause
        assert metrics["budget_exceeded_rate"] == 0.0, metrics
        assert out["data"]["runs"][0]["errors"] == 1, out["data"]["runs"][0]
        code, traces = _traces(root, out["data"]["group"])
        assert code == 0, traces
        error = traces["data"]["rows"][0]["error"]
        assert error.startswith("predict: TimeoutError"), error
        assert "budget_exceeded" not in error, error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c3_max_seconds_cannot_interrupt_a_sync_predict():
    """The documented caveat: only an *async* predict can be cut off (the row limit sees no clock)."""
    root = proj.project(
        config=SLOW_CONFIG.replace("slow_app:build_predict_fn", "slow_app:build_predict_fn_sync"),
        files={"slow_app.py": SLOW_APP},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        assert out["data"]["runs"][0]["errors"] == 0, out["data"]["runs"][0]
        code, traces = _traces(root, out["data"]["group"])
        assert code == 0 and traces["data"]["rows"][0]["error"] is None, traces["data"]["rows"][0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# a fake app that emits synthetic OTel spans through the REAL CaptureProcessor
# ---------------------------------------------------------------------------
PLANTED = "PLANTED-OTEL-SECRET-7f3a"
OTEL_APP = f'''
"""Emits synthetic OTel spans through the real CaptureProcessor (no SDK is installed)."""

import types

import evalops
from evalops.traces.capture import CaptureProcessor

PROCESSOR = CaptureProcessor()


def _span(i: int):
    ctx = types.SimpleNamespace(trace_id=99, span_id=100 + i)
    span = types.SimpleNamespace(
        name="chat gpt-4o",
        attributes={{
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 8,
            "weird.vendor.attr": "{PLANTED}",
            "vendor.secret.attr": "{PLANTED}",
        }},
        start_time=1_000 + i,
        end_time=1_010 + i,
        events=[],
        parent=None,
        status=None,
    )
    span.get_span_context = lambda: ctx
    return span


def behavior_config():
    return {{"llm_endpoint": "fake-llm@1", "tools_manifest_sha": "none"}}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        for i in range(4):
            span = _span(i)
            PROCESSOR.on_start(span)
            PROCESSOR.on_end(span)
        return "ok"

    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [noop]


def judge_meta():
    return {{"llm_judge_count": 0}}
'''

_ROW_LIMITS = "eval: {max_workers: 2, max_scorer_workers: 1, retries: 0, backoff_seconds: 0.01, "
OTEL_CONFIG = proj.BASE_CONFIG.replace("fake_app:", "trace_otel_app:").replace(
    "eval: {max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}",
    _ROW_LIMITS + "row_limits: {max_steps: 2, max_seconds: 60}}",
)
SLOW_CONFIG = proj.BASE_CONFIG.replace("fake_app:", "slow_app:").replace(
    "eval: {max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}",
    _ROW_LIMITS + "row_limits: {max_seconds: 0.05}}",
)

SLOW_APP = '''
"""Slow predict hooks: async (cut off by max_seconds) and sync (not interruptible)."""

import asyncio
import time

import evalops


def behavior_config():
    return {"llm_endpoint": "slow-llm@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    async def predict(question: str) -> str:
        await asyncio.sleep(0.3)
        return "too late"

    return predict


def build_predict_fn_sync(prompts):
    def predict(question: str) -> str:
        time.sleep(0.2)
        return "too late"

    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [noop]


def judge_meta():
    return {"llm_judge_count": 0}
'''
