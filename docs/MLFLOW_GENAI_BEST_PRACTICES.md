# MLflow 3 GenAI Best Practices (Agent Instructions)

Scope: tracing, evaluation and LLM judges, prompt registry, evaluation datasets, and app versioning with MLflow ≥ 3.1. These rules apply to both local MLflow servers and Databricks-managed MLflow.

> **Agent rule zero:** MLflow's GenAI APIs change quickly. Before you use an API, environment variable, or limit named here, check it against the installed version (`pip show mlflow`, `help(mlflow.genai.evaluate)`) or the docs at mlflow.org / docs.databricks.com. If something here conflicts with the installed version, the installed version wins. Flag the conflict to the user.

---

## 1. Environment & Setup

- Pin the MLflow version in `requirements.txt` / `pyproject.toml`. Use `mlflow>=3.1,<4` locally and `mlflow[databricks]>=3.1,<4` on Databricks. Never float to `latest` in CI.
- **Local backend:** Use `mlflow server` with a SQL backend. Postgres is preferred. SQLite is acceptable only for single-user, low-concurrency work. Never use the file store for GenAI evaluation, because evaluation datasets require a SQL backend.
- **Databricks:** Prompts and evaluation datasets live in Unity Catalog. Names are three-level (`catalog.schema.name`). The agent needs `CREATE FUNCTION`, `EXECUTE`, and `MANAGE` on the schema to create prompts.
- Keep a single config module (`mlflow_config.py`) that resolves names per environment. Code must never hardcode UC names or local names inline.

```python
# mlflow_config.py
import os
ENV = os.getenv("APP_ENV", "local")          # local | databricks
CATALOG, SCHEMA = "ml", "genai_eval"

def uc(name: str) -> str:
    return f"{CATALOG}.{SCHEMA}.{name}" if ENV == "databricks" else name
```

- Set the experiment explicitly in every entrypoint with `mlflow.set_experiment(...)`. Never rely on the default experiment.

---

## 2. Tracing

- Enable autologging for each SDK in use, for example `mlflow.openai.autolog()` or `mlflow.langchain.autolog()`. Add `@mlflow.trace` to your own functions, such as retrieval, tool calls, and post-processing, so spans are meaningful.
- Each user request must produce **exactly one trace**. `evaluate()` validates this.
- Tag traces with business context: `user_segment`, `session_id`, `feature_flag`, `app_version`. You will filter on these later in `mlflow.search_traces(filter_string=...)`.
- **Never log secrets or raw PII** into span inputs or outputs. Redact or hash before you trace. Treat traces as data that other people can read.
- In production, sample traces if volume is high. Always keep 100% of traces that are errored or flagged.

---

## 3. Versioning the Whole Application (not just the prompt)

**Core rule:** Anything that can change model behavior is part of the version. That includes the prompt, code, model endpoint, tools, retriever index, guardrails, feature flags, and the judge.

- Create a version identifier from the git SHA plus a hash of the behavior-affecting config, and set it as the active model **before** any traced call:

```python
import hashlib, json, mlflow
from mlflow.utils.git_utils import get_git_commit

behavior_config = {
    "llm_endpoint": "<endpoint>@<pinned-version>",
    "prompt_uri": "prompts:/ml.genai_eval.support/7",   # explicit version, not alias
    "tools_manifest": tool_schemas_as_json,              # names + JSON schemas
    "retriever_index_version": "<index snapshot id>",
    "guardrails": "<guardrail config id>",
    "feature_flags": active_flags,
}
cfg_hash = hashlib.sha256(json.dumps(behavior_config, sort_keys=True).encode()).hexdigest()[:8]
sha = (get_git_commit(".") or "nogit")[:8]

mlflow.set_active_model(name=f"support-agent-{sha}-{cfg_hash}")
mlflow.log_model_params({k: str(v)[:500] for k, v in behavior_config.items()})
```

- **Pin model versions** wherever the platform allows it. Do not use floating `latest` endpoints in evaluation.
- If the config hash changes, the change must be treated as a new version, even if no prompt or code changed. This is how platform-side changes stay visible.

---

## 4. Prompt Registry

- Register every production prompt, both app prompts **and judge prompts**, with `mlflow.genai.register_prompt(...)`. Include a commit message describing the change.
- The **app at runtime** loads by alias: `prompts:/<name>@production`.
- **Evaluation runs** load by explicit version: `prompts:/<name>/7`. Never evaluate against a moving alias.
- Promote a version by moving the alias (`mlflow.genai.set_prompt_alias`) **only after** its eval passes the gates in §8.
- Use the aliases `production`, `staging`, and `candidate`. Do not invent others without updating this doc.
- Never edit a prompt in place. A change always means a new version.

---

## 5. Evaluation Datasets

- Create datasets with `mlflow.genai.datasets.create_dataset(...)` and grow them with `dataset.merge_records(...)` from traces, DataFrames, or labeled examples.
- Maintain separate datasets by purpose:
  - `golden_regression` is **frozen**. The agent must never mutate it. Changes require explicit human approval and a new dataset name with a version suffix, such as `golden_regression_v2`.
  - `discovery` grows from production failures and labeled traces.
  - `adversarial_safety` holds jailbreaks, PII probes, and out-of-scope requests.
- Respect platform limits. Databricks has documented a per-dataset row cap of 2,000 rows at time of writing, so verify it. Split datasets by purpose rather than building one giant table.
- Store **expectations** (ground truth, from humans) separately from **feedback** (judge or human scores). Never write judge output back as an expectation.
- Record the dataset name and version (or Delta table version) as a param on every evaluation run.
- Deduplicate on normalized inputs when merging. Keep dataset inputs out of few-shot examples and prompt text to avoid leakage.

---

## 6. Judges & Scorers

- Prefer built-in scorers (`Correctness`, `RelevanceToQuery`, `Safety`, `Guidelines`, and so on) before writing custom ones.
- Always pin the judge setup:
  - Pin the judge model to an explicit version.
  - Set `inference_params={"temperature": 0.0}` for determinism.
  - Keep the judge prompt or instructions in the registry or in code under version control.
- **Never change the judge and the app in the same comparison.** Change one side at a time.
- Custom scorers must be **pure and thread-safe**. They run in a thread pool, so do not share mutable state or non-thread-safe clients across calls. Create the client per call or use a thread-safe client.
- Custom scorers must handle missing fields and return a well-formed result instead of raising.
- Calibrate each LLM judge against a sample of human labels, at least 50 rows, before you trust it. Re-calibrate when the judge model or judge prompt changes. Use MLflow's judge-alignment features where available.
- To debug a judge, set `MLFLOW_GENAI_EVAL_ENABLE_SCORER_TRACING=true` and inspect the scorer's own trace.

---

## 7. Running Evaluations: Concurrency, Async, Batching

### 7.1 Concurrency rules
- **Never** call `mlflow.genai.evaluate()` from multiple threads in one process. MLflow's fluent state (active run, model, experiment) is global. Run one `evaluate()` per process and let its internal pool handle the parallel work.
- Control parallelism with environment variables, not with your own thread pools:

```bash
export MLFLOW_GENAI_EVAL_MAX_WORKERS=4          # rows in parallel (default 10)
export MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS=2   # scorers per row (default 10); 1 = sequential
```

- Peak concurrent judge calls ≈ `MAX_WORKERS × min(MAX_SCORER_WORKERS, n_scorers)`. Keep this value below the judge endpoint's rate limit, with headroom for production traffic that shares the endpoint.

### 7.2 Async
- An async `predict_fn` is supported and is wrapped automatically. Set `MLFLOW_GENAI_EVAL_ASYNC_TIMEOUT` (seconds, default 300) for slow agents. In notebooks, `pip install nest_asyncio`.
- Keep scorers synchronous unless the installed version documents async scorer support.

### 7.3 Decouple generation from judging (default pattern)
Generate once, then judge the stored traces. This makes judging cheap to retry and lets you re-score old outputs with a new judge.

```python
# Phase 1: generation (expensive, run once per app version)
with mlflow.start_run(run_name=f"gen-{version}"):
    mlflow.genai.evaluate(data=dataset, predict_fn=predict_fn, scorers=[])

# Phase 2: judging (retryable, chunkable)
traces = mlflow.search_traces(run_id=gen_run_id)
for chunk in chunks(traces, size=200):
    with mlflow.start_run(run_name=f"judge-{version}-{judge_version}"):
        mlflow.genai.evaluate(data=chunk, scorers=SCORERS)
```

- Wrap each judging chunk in retry with exponential backoff for 429 and 5xx errors. Log failed chunk IDs and resume from them. Do not restart everything.
- If `predict_fn` is known to emit one trace per call, set `MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION=true` to skip the extra validation call.

### 7.4 Run naming & params
Every evaluation run must log these params:
`app_version`, `config_hash`, `prompt_uri` (explicit version), `dataset_name`, `dataset_version`, `judge_model`, `judge_prompt_version`, `mlflow_version`.

---

## 8. Regression Gating & Statistics

- Treat scores as noisy. For gating decisions, run the golden set **at least 3 times** and compare means with confidence intervals, not single runs.
- **Control run:** When the platform changes (endpoint upgrade, tool change, index rebuild), re-run the **previous** app version on the frozen golden set. If the control's scores move, the drift is in the platform or the judge, not the app change.
- Gate promotion on per-scorer thresholds and "no regression beyond X points vs. current production". Also check per-segment results so an average does not hide a regression in one slice.
- Track cost and latency per version alongside quality. A quality win that doubles cost needs explicit sign-off.

---

## 9. Production Monitoring & Feedback Loop

- Run a subset of scorers continuously on sampled production traces. Use Databricks production monitoring or a scheduled job that runs `search_traces → evaluate`.
- Collect user and expert feedback as assessments on traces, for example with `mlflow.log_feedback` or labeling sessions on Databricks.
- Funnel low-scoring or negatively-rated traces into the `discovery` dataset weekly. Promote reviewed cases into a new golden version only with human approval.

---

## 10. Local ↔ Databricks Parity

- All names go through `mlflow_config.uc()`, as in §1.
- Do not assume feature parity between local and Databricks. Some features (UC governance, managed monitoring, labeling UI, row limits) exist only on one side. Feature-detect, or guard with `ENV` checks.
- Run the same golden-set evaluation in both environments when you migrate. Expect small differences if the judge endpoints differ, and record which judge each run used.

---

## 11. Agent Do / Don't Checklist

**Do**
- Pin MLflow, model, prompt, judge, and dataset versions in every eval.
- Use the generate-then-judge pattern with chunked, retryable judging.
- Tune concurrency through environment variables to stay under rate limits.
- Log the full param set in §7.4.
- Verify APIs against the installed version before use.

**Don't**
- Don't parallelize `evaluate()` calls with your own threads.
- Don't evaluate against prompt aliases or floating model endpoints.
- Don't mutate the golden dataset or overwrite prompt versions.
- Don't change app and judge in the same comparison.
- Don't log secrets or raw PII into traces.
- Don't write judge outputs into expectations.

---

## 12. Enforced Versioning via `evalops` (CI- and backend-agnostic)

All versioning, evaluation, gating, and promotion goes through the `evalops` CLI. See `evalops/README.md`, `evalops/AGENTS.md`, and `evalops/docs/PLUGINS.md`.

**Roles and plugins.** The work is split into five roles: `prompts`, `datasets`, `ledger`, `tracer`, `evaluator`. Each environment in `evalops.yaml` picks a plugin per role:
- MLflow (local or Databricks) can serve all five.
- Langfuse and LangSmith serve prompts, datasets, tracer, and evaluator. Pair them with the `sql` or `mlflow` ledger.
- Logfire is a tracer only. Pair it with the `core` evaluator and `git` for prompts and datasets.
- Third-party plugins register under the `evalops.plugins` entry point, or are referenced as `module:Class`.

Any operation a plugin can't do exits with code 6 instead of failing silently.

**Stages**

| Stage | Command | Trigger |
|---|---|---|
| PR | `bash ci/pipeline.sh pr` runs guard → sync (immutable PR versions) → commit locks → plan → eval `smoke` → gate → PR comment | Every PR push |
| Main | `bash ci/pipeline.sh main` runs guard → sync check → eval `golden_regression` ×N → gate → `@candidate` | Merge |
| Release | `bash ci/pipeline.sh release` → `@production`. Requires an evidence group that passed the gate; that group becomes the baseline | Manual approval |
| Nightly | `bash ci/pipeline.sh nightly` checks out the baseline SHA, re-runs it, then runs `gate --label drift` | Schedule |

**Invariants**
- Prompts and datasets are files in git.
- Backend prompt versions and dataset snapshots (`<name>__<hash8>`) are immutable.
- Lock files are per environment.
- The behavior-config hash covers model, tools, index, flags, judge, and plugins, and is part of every app version name.
- Promotion requires a promotable group that passed the gate. Frozen-dataset changes require an approval label.
- Only CI credentials can write to shared backends.

## 13. Eval Cost Management

**Know the cost before running.** `evalops plan` reports LLM calls and USD:

LLM calls ≈ rows × repeats × (gen calls per row + LLM judges)

Estimates use the production baseline's measured tokens per row, or config defaults if there is no baseline yet.

**Enforce budgets.** `budgets.per_run_usd` and `budgets.per_month_usd` block runs with exit code 3. `--override-budget` is for humans only and is tagged on the run.

**Measure every run.** Each run logs these metrics:
- `gen_input_tokens` and `gen_output_tokens`
- `gen_cost_usd`: from `evalops.report_usage()` if called, otherwise the tracer's reported cost (MLflow ≥ 3.10, Langfuse, LangSmith), otherwise tokens × price table
- `judge_cost_usd_est`
- `total_cost_usd`
- `cost_per_row_usd`

Runs with `rows_missing_usage > 0` under-report generation cost. Fix the tracer integration, or call `evalops.report_usage()` in `predict_fn`.

**Gate on cost.** Use `max_cost_per_row_usd` and `max_cost_per_row_increase_pct` alongside the quality gates.

**Levers, cheapest first**
1. `--judge-only` re-scoring of stored traces. This has zero generation cost.
2. A small `smoke` set for PRs.
3. Fewer LLM judges per row. Replace them with code-based scorers where possible.
4. A cheaper judge model, aligned and calibrated against human labels.
5. Repeats only where decisions are made (main, nightly).

**Source of truth.** Provider or Databricks billing is the source of truth. Reconcile monthly and update `prices_per_1m_tokens` when providers change prices.

**Isolation.** Give CI evals their own API keys or endpoint quota, so eval spend and rate limits never affect production.

---

## 14. Agentic Traces & LLM Evaluation

For agents, an eval case is input → trajectory → output. evalops captures the trajectory in-process, normalizes it, and evaluates it. See `evalops/docs/AGENT_TRACES.md`.

**Capture**
- Capture through OpenTelemetry where possible (ADK, Strands, Pydantic AI, Semantic Kernel natively; CrewAI, LlamaIndex, smolagents, DSPy via OpenInference).
- LangGraph/LangChain uses native callbacks (`evalops.traces.langchain_callbacks()`). The OpenAI Agents SDK uses its trace processor.
- Use one capture path per framework, or every span is recorded twice.
- Your exporters to Langfuse, Logfire, MLflow, Phoenix, or Datadog keep working.

**Normalize.** All dialects map into one `AgentTrace`: OTel GenAI conventions (still Development status, moved to their own repository in June 2026), OpenInference, Traceloop, and the native adapters. Scorers never touch raw attributes. When an instrumentor is upgraded, add a real span dump to `tests/test_traces.py`.

**Score cheapest first**
1. Outcome, or `expected_state` for agents that act.
2. Deterministic trajectory checks: tool sequence, recall and precision, arguments, forbidden tools, loops, step budget, agent route.
3. Step-level or whole-trajectory LLM judges, only where deterministic checks can't decide.

Trace metrics (`trace.tool_calls`, `trace.latency_s/p95`, tokens) are gateable.

**Determinism**
- Tools are recorded once and replayed in PR and main runs (`@evalops.replayable`). A replay miss is an error, because it means the trajectory changed.
- Nightly runs use live tools.
- The cassette fingerprint is part of the app version.

**Reliability.** Agents are non-deterministic, so gate on pass^k (the share of cases that succeed on *all* k repeats) and on CI lower bounds, not single-run means.

**Containment.** Set a per-row circuit breaker (`eval.row_limits`) *and* each framework's own hard stop: `recursion_limit`, `max_turns`, `max_iter`, `max_steps`, `UsageLimits`, `max_llm_calls`.

**Judges**
- Judges are versioned (`judge_meta` and prompts) and calibrated against human labels (`evalops calibrate`: agreement, kappa, MAE) before they can gate.
- Never change the judge and the app in the same PR.
- A model swap is a `behavior_config` change evaluated through the same pipeline.

**Cost.** Cost is priced per LLM span and per model from the captured trace, so multi-model agents are exact. Budget per task, and use the p95 rather than the mean.
