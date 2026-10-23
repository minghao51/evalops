"""Trace normalization and capture across frameworks, using faithful synthetic span shapes.

No framework or OpenTelemetry package is installed in CI for these tests: they pin evalops' mapping
of each dialect. When upgrading an instrumentation library, capture one real span dump and add it here.
"""
import json
import pathlib
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from evalops import gate, tools  # noqa: E402
from evalops.calibrate import _stats  # noqa: E402
from evalops.ports import RowResult  # noqa: E402
from evalops.scorers import (agent_route, expected_state, forbidden_tools, no_loops, step_budget,  # noqa: E402
                             tool_args, tool_precision, tool_recall, tool_trajectory)
from evalops.scoring import call_scorer, wants_trace  # noqa: E402
from evalops.traces import BudgetExceeded, langchain_callbacks, openai_agents_processor, row_capture  # noqa: E402
from evalops.traces.capture import CaptureProcessor  # noqa: E402
from evalops.traces.model import AgentTrace  # noqa: E402
from evalops.traces.normalize import normalize  # noqa: E402


def trace_of(raws):
    return AgentTrace(spans=[normalize(r, i) for i, r in enumerate(raws)], source="otel")


def raw(sid, name, parent=None, t=0, **attrs):
    return {"span_id": sid, "parent_id": parent, "name": name, "start_ns": t, "end_ns": t + 10, "attributes": attrs}


# ---------------- dialects ----------------
def test_otel_genai_google_adk_shape():
    tr = trace_of([
        raw("1", "invoke_agent weather_agent", t=0, **{"gen_ai.operation.name": "invoke_agent",
                                                       "gen_ai.agent.name": "weather_agent"}),
        raw("2", "call_llm", "1", t=1, **{"gen_ai.request.model": "gemini-2.5-flash",
                                          "gen_ai.usage.input_tokens": 200, "gen_ai.usage.output_tokens": 20}),
        raw("3", "execute_tool get_weather", "1", t=2, **{"gen_ai.operation.name": "execute_tool",
                                                          "gen_ai.tool.name": "get_weather",
                                                          "gcp.vertex.agent.tool_call_args": '{"city": "Paris"}',
                                                          "gcp.vertex.agent.tool_response": '{"temp": 21}'}),
    ])
    assert tr.tool_names() == ["get_weather"] and tr.tool_calls()[0].inputs == {"city": "Paris"}
    assert tr.tool_calls()[0].outputs == {"temp": 21}
    assert tr.agent_route() == ["weather_agent"] and tr.tokens() == (200, 20)
    assert tr.llm_calls()[0].model == "gemini-2.5-flash"


def test_openinference_langgraph_shape():
    tr = trace_of([
        raw("1", "LangGraph", t=0, **{"openinference.span.kind": "CHAIN"}),
        raw("2", "supervisor", "1", t=1, **{"openinference.span.kind": "CHAIN",
                                            "metadata": json.dumps({"langgraph_node": "supervisor"})}),
        raw("3", "ChatOpenAI", "2", t=2, **{"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o",
                                            "llm.token_count.prompt": 50, "llm.token_count.completion": 5}),
        raw("4", "researcher", "1", t=3, **{"openinference.span.kind": "CHAIN",
                                            "metadata": json.dumps({"langgraph_node": "researcher"})}),
        raw("5", "search", "4", t=4, **{"openinference.span.kind": "TOOL", "tool.name": "search",
                                        "input.value": '{"q": "evals"}', "output.value": "results"}),
    ])
    assert tr.agent_route() == ["supervisor", "researcher"]
    assert tr.tool_names() == ["search"] and tr.tool_calls()[0].inputs == {"q": "evals"}
    assert tr.tokens() == (50, 5) and tr.steps == 2


def test_traceloop_crewai_and_pydantic_ai_shapes():
    tr = trace_of([
        raw("1", "Researcher.agent", t=0, **{"traceloop.span.kind": "agent", "traceloop.entity.name": "Researcher"}),
        raw("2", "web_search.tool", "1", t=1, **{"traceloop.span.kind": "tool", "traceloop.entity.name": "web_search",
                                                 "traceloop.entity.input": '{"query": "x"}'}),
    ])
    assert tr.tool_names() == ["web_search"] and tr.tool_calls()[0].inputs == {"query": "x"}
    pa = trace_of([
        raw("1", "agent run", t=0, agent_name="support"),
        raw("2", "chat gpt-4o", "1", t=1, **{"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 9}),
        raw("3", "running tool", "1", t=2, **{"gen_ai.tool.name": "roll_die", "tool_arguments": '{"sides": 20}'}),
    ])
    assert pa.agent_route() == ["support"] and pa.tool_names() == ["roll_die"]
    assert pa.tool_calls()[0].inputs == {"sides": 20} and pa.tokens() == (9, 0)


# ---------------- OTel processor routing ----------------
def _fake_otel_span(trace_id, span_id, parent_id, name, attrs, error=None):
    ctx = types.SimpleNamespace(trace_id=trace_id, span_id=span_id)
    s = types.SimpleNamespace(name=name, attributes=attrs, start_time=span_id, end_time=span_id + 1, events=[],
                              parent=types.SimpleNamespace(span_id=parent_id) if parent_id else None,
                              status=types.SimpleNamespace(status_code="StatusCode.ERROR" if error else "UNSET",
                                                           description=error))
    s.get_span_context = lambda: ctx
    return s


def test_capture_processor_routes_spans_to_row():
    proc = CaptureProcessor()
    with row_capture("00001") as cap:
        a = _fake_otel_span(7, 1, None, "invoke_agent a", {"gen_ai.operation.name": "invoke_agent"})
        b = _fake_otel_span(7, 2, 1, "execute_tool t", {"gen_ai.operation.name": "execute_tool",
                                                         "gen_ai.tool.name": "t"}, error="boom")
        proc.on_start(a)
        proc.on_start(b)
    proc.on_end(b)  # ends after the row context exited: routed via span id map
    proc.on_end(a)
    tr = cap.trace()
    assert tr.tool_names() == ["t"] and tr.tool_calls()[0].error == "boom" and tr.metrics()["tool_errors"] == 1


# ---------------- LangChain / LangGraph callbacks + circuit breaker ----------------
def test_langchain_handler_and_budget():
    import uuid
    with row_capture("r", {"max_steps": 2}) as cap:
        (h,) = langchain_callbacks()
        root, node, llm, tool = (uuid.uuid4() for _ in range(4))
        h.on_chain_start({"name": "LangGraph"}, {}, run_id=root, name="LangGraph")
        h.on_chain_start({}, {}, run_id=node, parent_run_id=root, metadata={"langgraph_node": "agent"}, name="agent")
        h.on_chat_model_start({}, [["hi"]], run_id=llm, parent_run_id=node, invocation_params={"model": "m"})
        gen = types.SimpleNamespace(text="ok", message=types.SimpleNamespace(
            content="ok", usage_metadata={"input_tokens": 11, "output_tokens": 3}))
        h.on_llm_end(types.SimpleNamespace(generations=[[gen]], llm_output={}), run_id=llm)
        h.on_tool_start({"name": "search"}, "q", run_id=tool, parent_run_id=node, inputs={"q": 1})
        h.on_tool_end(types.SimpleNamespace(content="res"), run_id=tool)
        try:
            h.on_tool_start({"name": "search"}, "q", run_id=uuid.uuid4(), parent_run_id=node)
            raise AssertionError("budget not enforced")
        except BudgetExceeded:
            pass
        h.on_chain_end({}, run_id=node)
        h.on_chain_end({}, run_id=root)
    tr = cap.trace()
    assert tr.agent_route() == ["agent"] and tr.tool_names() == ["search"] and tr.tokens() == (11, 3)
    assert tr.tool_calls()[0].outputs == "res" and tr.llm_calls()[0].model == "m"
    assert langchain_callbacks() == []  # outside an eval row: no-op


# ---------------- OpenAI Agents SDK ----------------
def test_openai_agents_processor():
    p = openai_agents_processor()
    T = types.SimpleNamespace
    with row_capture("r") as cap:
        p.on_trace_start(T(trace_id="tr1"))
        spans = [
            T(trace_id="tr1", span_id="s1", parent_id=None, started_at="2026-01-01T00:00:00+00:00",
              ended_at="2026-01-01T00:00:02+00:00", error=None, span_data=T(type="agent", name="Triage")),
            T(trace_id="tr1", span_id="s2", parent_id="s1", started_at="2026-01-01T00:00:00.5+00:00",
              ended_at="2026-01-01T00:00:01+00:00", error=None,
              span_data=T(type="response", name=None, model=None,
                          response=T(usage=T(input_tokens=40, output_tokens=8)))),
            T(trace_id="tr1", span_id="s3", parent_id="s1", started_at="2026-01-01T00:00:01+00:00",
              ended_at="2026-01-01T00:00:01.2+00:00", error=None,
              span_data=T(type="handoff", from_agent="Triage", to_agent="Billing")),
            T(trace_id="tr1", span_id="s4", parent_id="s1", started_at="2026-01-01T00:00:01.3+00:00",
              ended_at="2026-01-01T00:00:01.4+00:00", error={"message": "denied"},
              span_data=T(type="function", name="refund", input='{"id": 1}', output=None)),
        ]
        for s in spans:
            p.on_span_start(s)
            p.on_span_end(s)
        p.on_trace_end(T(trace_id="tr1"))
    tr = cap.trace()
    assert tr.agent_route() == ["Triage"] and tr.tool_names() == ["refund"] and tr.tokens() == (40, 8)
    assert len(tr.handoffs()) == 1 and tr.tool_calls()[0].error == "denied"


# ---------------- trajectory scorers ----------------
def test_trajectory_scorers():
    tr = trace_of([
        raw("1", "invoke_agent sup", t=0, **{"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "sup"}),
        raw("2", "execute_tool lookup", "1", t=1, **{"gen_ai.operation.name": "execute_tool",
                                                     "gen_ai.tool.call.arguments": '{"id": "A1"}'}),
        raw("3", "execute_tool search", "1", t=2, **{"gen_ai.operation.name": "execute_tool"}),
        raw("4", "execute_tool refund", "1", t=3, **{"gen_ai.operation.name": "execute_tool",
                                                     "gen_ai.tool.call.arguments": '{"id": "a1", "amt": 5}'}),
    ])
    exp = {"expected_tools": ["lookup", {"name": "refund", "args": {"id": "A1"}}], "forbidden_tools": ["delete"],
           "max_steps": 3, "expected_agents": ["sup"], "expected_state": {"status": "done"}}
    s = lambda f, **kw: call_scorer(f, inputs={}, outputs=kw.get("outputs"), expectations=kw.get("exp", exp),  # noqa
                                    trace=tr)
    assert s(tool_trajectory("in_order")) == {"tool_trajectory": 1.0}
    assert s(tool_trajectory("exact")) == {"tool_trajectory": 0.0}
    assert s(tool_recall()) == {"tool_recall": 1.0}
    assert abs(s(tool_precision())["tool_precision"] - 2 / 3) < 1e-9
    assert s(tool_args()) == {"tool_args": 1.0}          # case-insensitive, partial args
    assert s(forbidden_tools()) == {"forbidden_tools": 1.0}
    assert s(step_budget()) == {"step_budget": 1.0}
    assert s(agent_route("exact")) == {"agent_route": 1.0}
    assert s(expected_state(), outputs={"status": "done"}) == {"expected_state": 1.0}
    assert s(tool_trajectory(), exp={}) == {}             # not applicable -> excluded
    assert s(no_loops()) == {"no_loops": 1.0}
    assert wants_trace(tool_trajectory()) and not wants_trace(expected_state())


# ---------------- cassettes ----------------
def test_cassette_record_replay_miss():
    calls = {"n": 0}

    @tools.replayable("t")
    def t(x: int, y: int = 2):
        calls["n"] += 1
        return {"sum": x + y}

    path = pathlib.Path(tempfile.mkdtemp()) / "c.jsonl"
    assert t(1) == {"sum": 3} and calls["n"] == 1      # outside eval: passthrough
    with tools.use(tools.Cassette(path, "record")) as cas:
        assert t(1) == {"sum": 3}
        cas.save()
    with tools.use(tools.Cassette(path, "replay")):
        assert t(x=1, y=2) == {"sum": 3} and calls["n"] == 2   # replayed (same bound args), no live call
        try:
            t(5)
            raise AssertionError("miss not detected")
        except tools.ToolReplayMiss:
            pass


# ---------------- reliability + calibration stats ----------------
def test_reliability_and_calibration_stats():
    def rows(*oks):
        return [RowResult(f"{i}", {}, scores={"traj": 1.0 if ok else 0.0}) for i, ok in enumerate(oks)]
    runs = [rows(True, True, False), rows(True, False, False), rows(True, True, False)]
    rel = gate.reliability(runs, [{"metric": "traj/mean", "min": 1.0}])
    assert rel["k"] == 3 and abs(rel["pass_hat_k"] - 1 / 3) < 1e-9 and abs(rel["pass_at_k"] - 2 / 3) < 1e-9
    st = _stats([(1, 1), (0, 0), (1, 0), (1, 1)], 0.5)
    assert st["agreement"] == 0.75 and st["n"] == 4 and 0 < st["kappa"] < 1
