# evalops plugins

evalops splits the work into five **roles**. Each role is served by one plugin, chosen per environment in `evalops.yaml`. Core code never imports a vendor SDK, so you can mix backends freely: for example, prompts in Langfuse, traces in Logfire, and the run ledger in MLflow.

| Role | What it does | Interface (`evalops.ports`) |
|---|---|---|
| `prompts` | Stores immutable prompt versions and moves aliases/labels | `PromptStore` |
| `datasets` | Stores content-addressed dataset snapshots | `DatasetStore` |
| `ledger` | Durable record of runs (params, metrics, tags, artifacts); powers baselines, gates, budgets | `Ledger` |
| `tracer` | Traces each row and reports token usage/cost | `Tracer` |
| `evaluator` | Runs predict + scorers over a dataset | `Evaluator` |

## Built-in plugins

| Plugin | prompts | datasets | ledger | tracer | evaluator | Install extra | Status |
|---|---|---|---|---|---|---|---|
| `git` | ✓ (files + aliases.json in repo) | ✓ (jsonl snapshots) | | | | none | tested e2e |
| `sql` | | | ✓ (SQLite stdlib; Postgres verified; MySQL via SQLAlchemy, budget reservation untested) | | | `sql` for non-SQLite | tested e2e on SQLite; contract-tested on Postgres |
| `core` / `none` | | | | ✓ (no-op) | ✓ (runner with retries, rate limit, async) | none | tested e2e |
| `mlflow` | ✓ | ✓ | ✓ | ✓ | ✓ native `mlflow.genai.evaluate` | `mlflow` / `databricks` | written to MLflow 3.x docs, not run here |
| `langfuse` | ✓ (labels) | ✓ | — | ✓ | ✓ native `run_experiment` | `langfuse` | written to current SDK docs, not run here |
| `langsmith` | ✓ read/write; alias move ✗ | ✓ | — | ✓ | ✓ native `client.evaluate` (LangSmith datasets) | `langsmith` | written to SDK reference, not run here |
| `logfire` | — | — | — | ✓ (+ usage via Query API) | — | `logfire` | written to docs, not run here |

Lines marked `VERIFY` in each plugin are the API details most likely to change between SDK versions. Before relying on a vendor plugin, run `evalops doctor`, then a `dev` eval on `smoke`.

**Unsupported operations fail loudly.** If you ask a plugin for something it can't do, evalops exits with code `6` and suggests an alternative. Examples: LangSmith can't move commit tags automatically, and Logfire has no datasets.

**Native vs core evaluator:**
- The native evaluators (`mlflow`, `langfuse`, `langsmith`) give you the backend's own experiment UI, with traces and scores linked together.
- The `core` evaluator works with *any* tracer, and gives you identical retry and rate-limit behavior everywhere.
- Judge-only re-scoring always uses core over stored outputs, so it never repeats generation.

**Scorers:**
- Write neutral scorers with `@evalops.scorer("name", llm=True|False)` and the signature `fn(*, inputs, outputs, expectations)`.
- Return a float, a bool, `"yes"`/`"no"`, a dict with `score`/`value`, or a list of those.
- Native evaluators adapt neutral scorers automatically.
- Backend-native scorers also work. For example, MLflow's `Correctness()` is callable with the same keywords, so the core evaluator can use it too.

**Usage and cost:** inside `predict_fn`, call `evalops.report_usage(input_tokens=..., output_tokens=..., cost_usd=...)` whenever your tracer can't report usage. App-reported usage takes precedence.

## Writing a plugin

```python
# my_pkg/braintrust_plugin.py
from evalops.ports import Plugin, Tracer, Usage

class BraintrustTracer(Plugin, Tracer):
    name = "braintrust"
    provides = ("tracer",)          # list every role you implement

    def __init__(self, cfg, options):
        super().__init__(cfg, options)   # options = plugin_config.braintrust from evalops.yaml
        ...

    def wrap(self, fn, *, run_id, row_id):
        def traced(**inputs):
            # open a span, call fn(**inputs), return (output, trace_id)
            ...
        return traced

    def usage(self, trace_ids):           # optional
        return {tid: Usage(input_tokens=..., output_tokens=..., cost_usd=None) for tid in trace_ids}
```

To register a plugin, use either of these:
- **Entry point (recommended for packages).** Add this to *your* `pyproject.toml`:
  ```toml
  [project.entry-points."evalops.plugins"]
  braintrust = "my_pkg.braintrust_plugin:BraintrustTracer"
  ```
  Then set `plugins: {tracer: braintrust}`.
- **Direct path, no packaging.** Set `plugins: {tracer: "my_pkg.braintrust_plugin:BraintrustTracer"}`.

**Contract rules every plugin must keep:**
- `register` always creates a **new** version and never overwrites.
- `write` is only called for a snapshot name that doesn't exist yet. Never mutate an existing snapshot.
- `find_runs(tags=...)` matches **all** given tags and returns runs newest first.
- `set_alias` moves one alias/label without removing other aliases from the target version.
- Raise `self._unsupported(op, hint)` instead of silently doing nothing.

Copy `tests/test_e2e_local.py` and point it at your plugin to test the full lifecycle.
