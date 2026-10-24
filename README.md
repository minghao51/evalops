# evalops

[![ci](https://github.com/minghao51/evalops/actions/workflows/ci.yml/badge.svg)](https://github.com/minghao51/evalops/actions/workflows/ci.yml)

evalops is a backend-agnostic tool for versioning, evaluation, gating, and cost control of LLM apps **and agents**.

> Status: phase 1 in progress — contract, typed config, versioned formats, frozen `--json` envelope
> and payload redaction (WP1.1) are done. Statistics (WP1.3), ledger integrity, test debt and trace
> model v2 are next — see [`HANDOFF.md`](HANDOFF.md) and
> [`docs/IMPLEMENTATION_MAP.md`](docs/IMPLEMENTATION_MAP.md).

- **Backends:** MLflow (local or Databricks), Langfuse, LangSmith, Logfire, plain git + SQL, or your own plugin.
- **Runs under:** GitHub, GitLab, Azure DevOps, Jenkins, Databricks jobs, cron, or by hand.
- **Used by:** humans, CI, and coding agents, through the same CLI.

## Architecture
```
            humans / CI / coding agents
                       │  evalops CLI (--json, stable exit codes)
                       ▼
  ┌──────────── evalops core (no vendor imports) ────────────┐
  │ prompts-as-code · datasets-as-code · locks per env        │
  │ version hash · budgets · cost · gate · promote · guard    │
  └───────┬──────────┬──────────┬──────────┬──────────┬───────┘
       prompts    datasets    ledger     tracer   evaluator     ← roles (evalops.ports)
          │          │          │          │          │
   git│mlflow│   git│mlflow│  sql│mlflow  mlflow│langfuse│   core│mlflow│
   langfuse│    langfuse│               langsmith│logfire│  langfuse│
   langsmith    langsmith               none│your plugin  langsmith
```
Each environment in `evalops.yaml` picks a plugin per role. See `docs/PLUGINS.md` for the capability matrix and how to write a plugin.

## Agents
evalops supports LangGraph, Google ADK, OpenAI Agents SDK, CrewAI, LlamaIndex, Pydantic AI, Strands, AutoGen, Semantic Kernel, smolagents, DSPy, and custom loops.
- **Capture:** every row's trajectory is captured in-process (LangChain callbacks, the OpenAI Agents processor, or OpenTelemetry) and normalized into one `AgentTrace`.
- **Scoring:** scorers check outcome, trajectory (tools, arguments, routes, loops, step budget), and individual steps, using the same code for every framework.
- **Tools:** tools are recorded once and replayed offline in CI (`@evalops.replayable`).
- **Gating:** gates use reliability across repeats (pass^k), confidence intervals, and trace metrics (tool calls, latency p95, cost per task).
- **Circuit breaker:** a per-row breaker contains runaway loops.
- **Judges:** `evalops calibrate` checks LLM judges against human labels before they gate anything.

See `docs/AGENT_TRACES.md`, `examples/langgraph_hooks.py`, and `examples/other_frameworks.md`.

## What is versioned
| Thing | Source of truth | Stored as | Pinned by |
|---|---|---|---|
| Prompts | `prompts/*.txt\|json` in git (`{{var}}` placeholders) | Immutable versions in the `prompts` backend | `.evalops/locks/<env>/prompts.lock.json` |
| Datasets | `datasets/*.jsonl` in git | Content-addressed snapshots `<name>__<hash8>`, never mutated | `.evalops/locks/<env>/datasets.lock.json` |
| App config (model, tools, index, flags, judge, plugins) | `behavior_config()` / `judge_meta()` hooks | Ledger params | Config hash in the app version name |
| Code | git | n/a | Git SHA on every run |

Locks are **per environment**, because the same git source maps to different version ids in each backend.

## Cost
- **Before a run:** `evalops plan` estimates LLM calls and USD. It uses the production baseline's measured tokens per row, or config defaults if there is no baseline yet.
- **Budgets:** `evalops eval` enforces `budgets.per_run_usd` and `budgets.per_month_usd` (exit code 3).
- **Per-row generation cost** comes from the first source available:
  1. `evalops.report_usage()` called in your app
  2. cost reported by the tracer (MLflow ≥ 3.10, Langfuse, LangSmith)
  3. tokens × the `prices_per_1m_tokens` price table
- **Judge cost** is estimated from configured per-call token sizes.
- **Logged metrics:** `gen_cost_usd`, `judge_cost_usd_est`, `total_cost_usd`, `cost_per_row_usd`, and `rows_missing_usage`.
- **Gating:** `evalops gate` fails on cost-per-row regressions as well as quality regressions.
- **Spend:** `evalops cost` shows month-to-date spend from the ledger.

## Quick start (no external services)
```bash
pip install -e .
evalops plugins                      # env 'local': git + sql + core
evalops sync all && git add -A && git commit -m "evalops locks"
evalops plan --dataset smoke --stage dev
evalops eval --dataset smoke --stage dev
evalops gate
```
With `--json`, stdout is exactly **one** envelope document and the process exit code equals `code`
([CONTRACT §2](docs/CONTRACT.md#2-the---json-envelope-v1)). On `evalops --json guard`:

```json
{
  "ok": true,
  "code": 0,
  "schema_version": 1,
  "command": "guard",
  "env": "local",
  "data": {"summary": "guard passed", "violations": []},
  "errors": []
}
```

On a failure `code` is `1`–`7` (never `0`), and `errors[0]` carries `{code, message, hint?, details?}`.

To switch backends, run `EVALOPS_ENV=mlflow|databricks|langfuse|langsmith|logfire`, install the matching extra (for example `pip install -e ".[langfuse]"`), and run `evalops doctor`.

## Pipeline
Every CI system calls `bash ci/pipeline.sh pr|main|release|nightly`. Adapters for GitHub, GitLab, and Azure DevOps are in `ci/`.
- **pr:** guard → sync (immutable PR versions) → commit locks → calibrate judges (if `datasets/judge_calibration.jsonl` exists) → plan → eval smoke with tool replay → gate → PR comment
- **main:** guard → `sync --check` → eval golden ×N → gate → promote `@candidate`
- **release:** manual approval → promote `@production` with `EVIDENCE_GROUP`. This group becomes the baseline.
- **nightly:** check out the baseline's git SHA → control eval → `gate --label drift`. This detects platform, model, or judge drift that no PR caused.

## Tests
- `make check` is the gate: ruff + mypy + pytest. It runs offline, with no docker, network, or vendor SDK. `make test` runs just the suite; `make lint`, `make typecheck`, and `make test-unit` are the parts.
- `python tests/run_without_pytest.py` is the dependency-free fallback runner (same tests, no pytest). Both must stay green.
- `tests/unit/test_traces.py` pins the span mapping for each framework dialect.
- `tests/integration/test_e2e_agent.py` runs a LangGraph-style multi-agent app through the full agent lifecycle: record/replay, the reliability gate catching a flaky router, the circuit breaker stopping a runaway loop, judge-only re-scoring of stored traces, and judge calibration.
- `tests/integration/test_e2e_local.py` covers the whole lifecycle on the zero-dependency plugins: sync, guard, async and flaky predict with retries, gate, promotion, regression detection, judge-only re-scoring, budgets, frozen datasets, capability errors, and a third-party plugin.
- `tests/unit/test_no_vendor_imports.py` AST-scans core and fails if a vendor SDK is imported outside `plugins/` and `traces/`.
- `make contract` runs the docker-local vendor contract suite; it is skipped when docker or `tests/contract/` is absent, and is excluded from `make check`.

## Known limits
- The vendor plugins (`mlflow`, `langfuse`, `langsmith`, `logfire`) follow current docs but were not run against live services when written. Lines marked `VERIFY` are the API details to check on SDK upgrades.
- LangSmith commit-tag moves are not automated, and evalops exits with code 6. Keep prompts in git, MLflow, or Langfuse if you need automated promotion.
- With the git plugin, alias moves are commits, so the main and release jobs need push rights (or a bot PR if branches are protected). Committing lock files back to a PR doesn't work for fork PRs.
- The SQLite ledger is local. In CI, use a shared ledger: `EVALOPS_LEDGER_URL=postgresql+psycopg://...` with the `sql` extra, or MLflow.
