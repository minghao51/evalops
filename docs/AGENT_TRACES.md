# Agentic traces & LLM evaluation in evalops

For agents, an eval case is **input → trajectory → output**. evalops captures the trajectory in-process, normalizes it into one framework-neutral model (`evalops.traces.AgentTrace`), and scores it with the same code in CI and on production traces.

```
 LangGraph ─ callbacks ─┐
 OpenAI Agents ─ processor ┤
 ADK / Strands / Pydantic AI / SK ─ native OTel ┤──► normalize ──► AgentTrace ──► scorers ──► gates
 CrewAI / LlamaIndex / smolagents / DSPy ─ OpenInference OTel ┤        (dialects:      (outcome,
 anything else ─ OTel GenAI conventions ┘                       GenAI semconv,    trajectory,
                         │                                      OpenInference,    step judges,
                         └─► your exporters keep working         Traceloop)        trace metrics)
                             (Langfuse, Logfire, Phoenix, MLflow, Datadog…)
```

## Framework support

| Framework | Capture path | What you add | Framework-native hard stop (use it as well) |
|---|---|---|---|
| **LangGraph / LangChain** | Native callbacks (recommended) | `graph.invoke(x, config={"callbacks": evalops.traces.langchain_callbacks(), "recursion_limit": 25})` | `recursion_limit` (and the evalops breaker is *enforced* here) |
| LangGraph / LangChain (alt.) | OpenInference OTel | `traces.instrument: [langchain]` | `recursion_limit` |
| **Google ADK** | Native OTel spans | `traces.capture: true` + `pip install opentelemetry-sdk` (or preset `google_adk`) | `RunConfig(max_llm_calls=…)` |
| **OpenAI Agents SDK** | Native trace processor | `agents.add_trace_processor(evalops.traces.openai_agents_processor())` | `Runner.run(…, max_turns=…)` |
| **CrewAI** | OpenInference OTel | `traces.instrument: [crewai, openai]` | `Agent(max_iter=…)` |
| **LlamaIndex** | OpenInference OTel | `traces.instrument: [llama_index]` | workflow timeout |
| **Pydantic AI** | Native OTel | `traces.instrument: [pydantic_ai]` | `UsageLimits(request_limit=…)` |
| **Strands Agents** | Native OTel | `traces.capture: true` | framework limits |
| **AutoGen / AG2** | OTel (core) or OpenInference | `traces.instrument: [autogen_agentchat]` | termination conditions |
| **Semantic Kernel** | Native OTel (enable its GenAI diagnostics) | `traces.capture: true` | function-choice limits |
| **smolagents** | OpenInference OTel | `traces.instrument: [smolagents]` | `max_steps` |
| **DSPy** | OpenInference OTel | `traces.instrument: [dspy]` | module limits |
| **Custom loop** | Any OTel GenAI spans + `evalops.traces.check_budget()` | Instrument your LLM client (`traces.instrument: [openai]`, `anthropic`, `litellm`, …) | your own `max_steps` |

**How the circuit breaker stops a run** — `evalops.traces.capture.BREAKER_PARITY` is the source of truth, and every row below has a regression test:

| Capture path | `BREAKER_PARITY` | What actually happens |
|---|---|---|
| `langchain` | `enforced` | `on_llm_start` / `on_tool_start` raise `BudgetExceeded` before the step, aborting the run. Also keep `recursion_limit`. |
| `otel` | `post-hoc` | Spans are only observed; `cap.exceeded` becomes the row's `budget_exceeded` error after predict. Keep the framework's own hard stop (right column above). |
| `openai_agents` | `post-hoc` | The SDK swallows processor exceptions, so the processor only sets `cap.exceeded` -> the same post-hoc row error. Set `max_turns`. |
| `custom` | `cooperative` | `evalops.traces.check_budget()` raises inside your loop; only a loop that calls it can be stopped. |

`row_limits.max_seconds` cuts off an **async** predict: the core evaluator cancels the coroutine that outlives the limit and the row records `predict: TimeoutError`, which is never relabelled as `budget_exceeded`. A **sync** predict cannot be interrupted (there is no timer watching it); it is only flagged when it records a span or step after the limit has already passed, so pair it with the framework's own hard stop.

**Verification status:**
- Tested end to end: the LangGraph callback path and every scorer, gate, cassette, and calibration feature.
- Tested with faithful synthetic spans: the OTel GenAI, OpenInference, Traceloop, ADK, Pydantic AI, and OpenAI Agents mappings (`tests/test_traces.py`). The frameworks themselves were not installed.
- The GenAI semantic conventions are still in *Development* status, so attribute names can change. All mapping lives in `traces/normalize.py`. When you upgrade an instrumentor, dump one real span and add it to `tests/test_traces.py`.
- Use **one** capture path per framework (for example, LangChain callbacks *or* the OpenInference LangChain instrumentor). Using both records every span twice.

## The normalized trace
`AgentTrace` provides:
- `tool_calls()`, `tool_names()`, `llm_calls()`
- `agent_route()`: sub-agents, graph nodes, and handoffs, in order
- `handoffs()`, `errors()`, `steps`, `tokens()`, `duration_s`
- `metrics()` and `render()`

Each `Span` has `kind` (agent, llm, tool, retrieval, embedding, chain, guardrail, handoff), `name`, `inputs`/`outputs`, `model`, `tool_name`, `agent_name`, tokens, timing, and `error`.

Traces are stored with every run (`evalops/rows.jsonl` in the ledger). To inspect them:
```bash
evalops traces --row 00003        # indented tree: [agent] supervisor > [tool] lookup_order args=… -> …
```

## Trace document version
The normalized trace is stored as a versioned document inside every `evalops/rows.jsonl` line, with its own `TRACE_SCHEMA_VERSION = 2` (`src/evalops/traces/model.py`, CONTRACT §5.6). The rows line keeps `FORMAT_VERSION = 1`; the two versions are independent.

| Nested `trace.schema_version` | Reader |
|---|---|
| absent | v1, the legacy shape: read fine |
| `1` or `2` | accepted; the object records the version it read |
| `3+`, `0`, a string, `null`, a bool | `FormatError` -> exit 7 naming the artifact (`evalops/rows.jsonl`, or `dataset:<name>` on the calibration path) |

`AgentTrace.to_dict()` always writes `2`, so re-writing a v1 row upgrades it. The version is metadata for rows and row identity: it never enters `app_version` or `row_key`, and v1 and v2 of the same trace produce identical `trace_metrics` and gate checks. A trace-less dataset fingerprint is unchanged; a dataset source record that embeds a trace document is fingerprinted verbatim, so upgrading an embedded trace is a new dataset snapshot by design. `AgentTrace.source` is exactly one of `otel`, `langchain`, `openai_agents`, `mixed`, `stored`, `unknown` (a document that omits it reads as `stored`).

### Unknown span attributes
`normalize()` knows the attributes it consumes (all GenAI/OpenInference/Traceloop aliases plus the kept ones). Every other raw attribute is recorded by **name only** — never by value, because a value may be a secret — sorted and deduplicated, in `Span.unknown_attributes`; `AgentTrace.unknown_attributes()` returns `{name: span_count}`.

```bash
evalops traces --row 00003 --unknown-attrs
# data.rows[i].unknown_attributes          -> ["weird.vendor.attr"]  (sorted names)
# data.unknown_attributes                  -> {"weird.vendor.attr": 1} (span count)
```

Without `--unknown-attrs` the payload is byte-identical to before the flag existed (its envelope golden is frozen); `--row` filtering still applies with the flag.

## Scoring at three levels (cheapest first)
1. **Outcome.** Any scorer over `outputs`, or `expected_state` for agents that act rather than answer.
2. **Trajectory (deterministic, nearly free).** These are in `evalops.scorers`:
   - `tool_trajectory(match=exact|in_order|any_order|subset)`
   - `tool_recall`, `tool_precision` (penalizes wasted calls)
   - `tool_args` (partial, normalized argument match)
   - `forbidden_tools`, `step_budget`, `tool_errors`, `no_loops`
   - `agent_route` (LangGraph supervisor routing, ADK sub-agents, OpenAI handoffs)
3. **Step-level LLM judges.** `span_judge(judge, kind="tool")` judges each selected span. `trajectory_judge(judge)` makes one holistic call per row over `trace.render()`. You supply the judge callable, so these stay model-neutral.

**Trace metrics are gateable too.** Each row reports `trace.steps`, `trace.tool_calls`, `trace.llm_calls`, `trace.latency_s`, tokens, and errors, aggregated as `/mean` and `/p95`. For example, `trace.tool_calls/mean: {max_increase_pct: 30}` catches a prompt change that keeps answers right but doubles tool usage.

**Dataset expectations for agents** (all optional):
```json
{"inputs": {"question": "Refund order A1"},
 "expectations": {"expected_tools": ["lookup_order", {"name": "issue_refund", "args": {"order_id": "A1"}}],
                  "tool_match": "in_order", "forbidden_tools": ["delete_account"], "max_steps": 6,
                  "expected_agents": ["supervisor", "refund_agent"], "expected_state": {"status": "refunded"}}}
```

## Tools: record once, replay in CI
Decorate the raw tool function, innermost:
```python
@tool                                   # LangChain/LangGraph @tool, ADK FunctionTool, CrewAI @tool, …
@evalops.replayable("lookup_order")
def lookup_order(order_id: str) -> dict: ...
```
- **Recording:** `evalops eval --dataset agent_golden --tool-mode record` calls live tools once and writes `cassettes/<dataset>.jsonl`. Commit it.
- **Stage defaults:** `tools.mode_by_stage` sets `pr`/`main` to `replay` (deterministic, offline, no side effects) and `nightly` to `live`.
- **Replay misses:** a call with no recording is an error. That is deliberate: it means the trajectory changed. Re-record the cassette, review it, and commit it.
- **Versioning:** the cassette fingerprint is part of the app version hash.
- **Outside evals:** the decorator is a no-op passthrough.

## Gating non-deterministic agents
Single-run averages hide flakiness, so evalops gates on per-case reliability across repeats:
```yaml
eval: {repeats: {main: 3}, row_limits: {max_steps: 12, max_tokens: 60000, max_seconds: 120}}
gates:
  reliability:
    success: [{metric: tool_trajectory/mean, min: 1.0}, {metric: expected_state/mean, min: 1.0}]
    min_pass_hat_k: 0.9        # share of cases that succeed on ALL k repeats
    max_drop_pass_hat_k: 0.05  # vs production baseline
  metrics:
    tool_trajectory/mean: {min_ci_lower: 0.85}   # 95% CI lower bound over row-level scores
```

## LLM evaluation: judges are versioned models too
- **Versioning:** a judge's model and prompt live in `judge_meta` and `prompts/`, and both are part of the app version hash.
- **Calibration:** `evalops calibrate --dataset judge_calibration` scores pre-recorded outputs (and traces) that have human labels, then reports agreement, Cohen's kappa, and MAE against `judges:` thresholds. Run it whenever a judge changes, and before that judge gates anything. The PR pipeline runs it automatically when the dataset exists.
- **Model comparisons** are just `behavior_config` changes run through the same pipeline: same datasets, same gates.

## Cost for agents
- **Measurement:** cost is measured per task from the captured trace. Each LLM span's tokens are priced with `cost.prices_per_1m_tokens.models.<model>`, so multi-model agents (a cheap router plus an expensive worker) are costed exactly.
- **Containment:** `row_limits` caps runaway loops, so they can't blow the budget.
- **Estimates:** `plan` uses measured tokens per row from the baseline.
