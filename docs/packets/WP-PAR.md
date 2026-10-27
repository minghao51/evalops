# WP3.0 packet — parity: metrics, usage, and the tracer role across evaluators (ADR 0008)

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **482 tests** (+1 contract, deselected).
Read `docs/adr/0008-evaluator-owned-tracing.md` FIRST (this WP implements its three code
consequences — the decision itself is made and not yours to relitigate), then
`src/evalops/plugins/core.py` (`evaluate`: per-row `collect()` usage, the `tracer.usage` backfill,
the `error_rate`/`budget_exceeded_rate` emission), `src/evalops/plugins/mlflow.py` (`evaluate`:
the `result_df` path, its tracer-role `usage()`), `src/evalops/run.py` (`_capturing`, the
`native` branch after `evaluator.evaluate`, `_stamp_judge_identities` — the pattern to follow),
`src/evalops/usage.py` (the ContextVar collector), `src/evalops/cli.py` (`doctor` dispatch),
`src/evalops/cost.py` (`measure`: how `rows_missing_usage` is computed — already backend-agnostic),
and `tests/unit/test_core.py` + `tests/unit/test_cli_commands.py` (the existing pins you must keep
green or replace deliberately).

**Dispatch order: this WP lands BEFORE WP3.1a** (the mlflow contract slice asserts the behaviors
you add here).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 482 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No vendor SDK into `.venv` (the AST guard stays green; mlflow-side behavior is fake-SDK-tested like
`test_native_judges.py`/`test_core.py` do it). No network, no docker.

## Goal

ADR 0008's three code consequences: metrics, usage, and tracer-backfill parity between the core
evaluator and vendor evaluators — plus the doctor honesty for self-tracing evaluators. After this
WP, an `evalops eval` through `evaluator: mlflow` behaves like core for everything downstream
consumers see: `error_rate`/`budget_exceeded_rate` present, `report_usage()` harvested,
`rows_missing_usage` filled from the configured tracer where possible.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. Universal metric emission (`run.py` owns it; `core.py` loses its private copy)

- **A1** `run.py`, immediately after `result = evaluator.evaluate(...)` (and after the native
  trace-attach/`_post_score` block so row errors are final): compute
  `result.metrics["error_rate"] = sum(1 for r in result.rows if r.error) / max(len(result.rows), 1)`
  and `result.metrics["budget_exceeded_rate"] = sum(1 for r in result.rows if (r.error or "").startswith("budget_exceeded")) / max(len(result.rows), 1)`
  — **unconditionally, for every evaluator**. Delete the two emission lines from
  `plugins/core.py::evaluate`; the values are identical for core (same rows, same formula), which
  is asserted by the untouched `eval.json`/`gate_pass.json`/`gate_failure.json` goldens.
- **A2** `plugins/mlflow.py::evaluate` maps per-row failures so `row.error` is real: from
  `result_df`, a per-row failure/status column (name is version-dependent — guard with
  `hasattr`/column-presence checks and mark `# VERIFY(...)` exactly like the neighboring
  `result_df` line) sets `row.error = f"predict: {<value>}"` when the row's prediction failed.
  A dataset where `predict` fails per-row must yield `error_rate == 1.0` (fake-SDK test), never a
  silently clean run. Rows the df marks failed have `outputs is None`-style absence handled the
  same way the core evaluator does (score skip).
- **A3** No other metrics move. `aggregate_rows`, scorer metrics, `cost.measure` (which already
  computes `rows_missing_usage` for every evaluator) stay put.

### B. App-reported usage under every evaluator (`run.py`)

- **B1** New wrapper `_collecting(fn, stash)` in `run.py` (the `_capturing` pattern): wraps the
  predict fn; per invocation opens `usage.collect()`, calls fn, then records
  `stash[canonical(inputs)] = Usage(...)` **only when the collector reported** (`usage.reported`),
  overwriting on retries (last attempt wins — same semantics as core's `caps[-1]`).
  `canonical`/inputs-keying is the proven `stash` trick from `_capturing`; identical-input rows
  share the last write (document in the docstring; core rows are unaffected by this because of B2).
- **B2** In `_evaluate`, wrap `predict_fn` with `_collecting` for **every** evaluator (compose with
  `_capturing` on the native path: `_collecting(_capturing(...))`; order irrelevant, they wrap
  different concerns). After `evaluator.evaluate` returns: for each row with
  `row.usage is None`, fill from the stash by `canonical(row.inputs)`. Core rows keep the exact
  per-row collector inside `core.py` (it stays; B never overwrites an existing usage).
- **B3** `_evaluate` builds the stash once per run and passes it down; the fill happens before the
  cost `measure`/artifact staging so `rows_missing_usage` reflects it.

### C. One usage-backfill point (`run.py`; `core.py` loses its private copy)

- **C1** Move the `tracer.usage(missing)` backfill block from `plugins/core.py::evaluate` to
  `run.py` right after B2's fill: collect rows with `row.usage is None and row.trace_id`, call
  `tracer.usage(missing)` on the **configured** tracer (the object already in scope), assign
  hits. Core behavior is unchanged (same tracer, same rows); vendor evaluators gain it. A miss
  (wrong backend, unreachable tracer) stays an honest `rows_missing_usage` — no exception
  surfaces from a failed lookup beyond what `tracer.usage` already raises/returns today.
- **C2** The mlflow evaluator's own internal usage backfill from MLflow traces
  (`self.usage(...)` in its `evaluate`) **stays** — it is the self-tracing evaluator filling from
  its own traces, exactly ADR 0008's model. C adds the configured-tracer chance for everything
  else.

### D. Doctor honesty for self-tracing evaluators (`cli.py`)

- **D1** In the `doctor` command dispatch: after the per-role report is built, if
  `"tracer" in cfg.plugins` and `cfg.plugins["tracer"] != cfg.plugins["evaluator"]` and the
  evaluator plugin declares `"tracer"` in its `provides` (read from the instantiated evaluator
  plugin — `provides` is the registry-checked truth), append to a new `warnings` list:
  `"evaluator '<evaluator>' self-traces (native tracing); the configured tracer '<tracer>' is not invoked for row tracing or usage backfill"`.
- **D2** The `warnings` key is present in `data` **only when non-empty** (additive; every existing
  golden — `doctor.json` included — stays byte-identical because the golden project's evaluator is
  `core`). A self-tracing evaluator with no separate tracer configured, and any core wiring, emit
  nothing.

### E. Blast-radius discipline

- **E1** No config/schema change (`make schema` zero diff), no capability-matrix change
  (`make capabilities` zero diff — metric emission is not matrix content), no preset change
  (`presets.py` untouched: finding 3 is resolved by A1 making the mlflow preset's `error_rate`
  gate truthful), no envelope shape change except D2's conditional `warnings`.
- **E2** Contract/docs updates (ADR 0008 already landed; CONTRACT §3 wording) are the
  orchestrator's, not yours — `docs/**` is forbidden.
- **E3** Existing tests that assert core-internal details (e.g. that `core.evaluate`'s returned
  metrics dict carries `error_rate`) are updated to the run-layer owner **deliberately** — name
  every such change in the report; the assertion target (values, not the computing module) must
  survive.

## Tests (`tests/unit/test_parity.py` new; targeted edits in `test_core.py`/`test_cli_commands.py`)

- **C1 (metrics)** A fake evaluator plugin (the `MemoryLedgerSource` pattern: a `module:Class`
  plugin in the test tree) whose rows carry `error` values → `eval` metrics contain the exact
  `error_rate`/`budget_exceeded_rate`; a clean fake vendor run → `0.0`/`0.0` present (not absent);
  the core path's values unchanged (existing tests + goldens).
- **C2 (mlflow row errors)** Fake-SDK `result_df` with a failure column → `row.error` set,
  `error_rate == 1.0`; df without the column → no error invented (VERIFY posture).
- **C3 (usage stash)** Fake vendor evaluator + app hook calling `evalops.report_usage` →
  `row.usage` filled, `rows_missing_usage == 0`, `gen_input_tokens` > 0; retry overwrite: a hook
  that reports twice across attempts → last attempt's numbers win; identical-input rows → last
  write (documented, pinned); core wiring → unchanged `rows_missing_usage: 0` behavior.
- **C4 (backfill)** A fake tracer whose `usage()` returns known ids → vendor rows missing usage get
  filled; a tracer returning nothing → honest `rows_missing_usage`; core path: the backfill fires
  exactly once per run (no double-call after the move).
- **C5 (doctor)** The full matrix: core + tracer → no `warnings` key; mlflow-style evaluator
  (declares tracer) + separate tracer → warning naming both; self-tracing evaluator with no
  configured tracer → nothing; `doctor.json` golden byte-identical.
- **C6 (mutations, on /tmp copies; report each red test)**: (i) move emission back into core only
  (drop the run.py block) → C1 vendor test red; (ii) drop `_collecting` → C3 red; (iii) drop the
  run.py backfill → C4 red; (iv) make the doctor warning unconditional (fires on core wiring too)
  → C5 red + the doctor golden test red.

## Falsifiable done-when

- **H1** `error_rate`/`budget_exceeded_rate` present for every evaluator, computed once in
  `run.py`; core's private emission deleted; all goldens byte-identical (`eval`, both gates).
- **H2** `evalops.report_usage` lands in `row.usage` under a vendor evaluator; retries overwrite;
  `rows_missing_usage` reflects the harvest.
- **H3** The configured tracer's usage backfill runs from `run.py` for every evaluator, exactly
  once per run; core behavior unchanged; misses stay honest.
- **H4** `doctor` warns (only) on self-tracing-evaluator + separate-tracer wirings, naming both
  plugins; `doctor.json` golden byte-identical; mlflow row-error mapping is `VERIFY`-marked and
  fake-SDK-tested both ways (column present/absent).
- **H5** Blast radius: `git status --short` inside the allowed paths only; `make schema`,
  `make capabilities`, `make ci-adapters` zero diff; vendor-import guard untouched; `presets.py`
  untouched.
- **H6** Gates: `make check` green (482 → new total), standalone runner green, `make contract`
  skips offline.

## Allowed paths

`src/evalops/run.py`, `src/evalops/plugins/core.py`, `src/evalops/plugins/mlflow.py`,
`src/evalops/cli.py` (the doctor warning only), `tests/unit/test_parity.py` (new),
`tests/unit/test_core.py`, `tests/unit/test_cli_commands.py`, and only if a fixture evaluator is
needed: a new `tests/fixtures/parity_vendor_app.py` (+ registration in the test that imports it by
path — no `pyproject.toml` change).

## Forbidden

`src/evalops/plugins/{langfuse,langsmith,logfire,sql,git}.py` (their parity lands with their
contract slices), `src/evalops/presets.py`, `src/evalops/config/**`, `envelope.py`, `scoring.py`,
`cost.py` (`measure` already universal — if you believe it must change, stop and report a
blocker), `gate.py`, `traces/**`, `docs/**` (ADR 0008 + CONTRACT wording are the orchestrator's),
`pyproject.toml`, `Makefile`, `ci/**`, any golden, deleting/skipping/loosening tests,
`git commit`/`push`/branch, installing anything into `.venv`, any real network call.

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format src tests
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
.venv/bin/python tests/run_without_pytest.py
make check
make contract
make schema && git status --short docs/
make capabilities && git status --short docs/
make ci-adapters && git status --short ci/
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Parity surface
<emission point; usage-stash mechanics incl. the retry/identical-inputs semantics; backfill move;
doctor warning rule; mlflow row-error mapping and its VERIFY mark>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the C6 mutation evidence and the golden-identity proof>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3.0 (evaluator parity per ADR 0008) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. The packet is `docs/packets/WP-PAR.md` — read it and
`docs/adr/0008-evaluator-owned-tracing.md` first, then `run.py`, `plugins/core.py`,
`plugins/mlflow.py`, `cli.py`, and `tests/unit/test_parity.py`.

- **H1 metrics.** Verify the emission moved (core.py has no `error_rate` line left) and is
  computed after row errors are final (post `_post_score`); values identical for core — prove the
  three goldens byte-identical yourself. Try to construct a vendor row set where run.py's numbers
  would diverge from core's formula.
- **H2 usage stash.** The inputs-keying semantics: retry overwrite (last attempt wins, like
  core's `caps[-1]`), identical-input rows share the last write (documented + pinned), fill only
  when `row.usage is None` (core's exact collector never clobbered), and the stash fills BEFORE
  cost measure so `rows_missing_usage` is truthful. Drive the fake vendor run yourself.
- **H3 backfill.** Exactly one backfill per run, on the configured tracer, for every evaluator;
  core's old block is gone (grep it); a miss is honest. Check the mlflow evaluator's OWN trace
  backfill survived (C2 keeps it — ADR 0008's self-tracing model).
- **H4 doctor.** The rule fires only on self-tracing evaluator + different configured tracer;
  `provides` is read from the instantiated plugin, not a name list; the key is absent (not `[]`)
  when empty; `doctor.json` byte-identical. Try core wiring, no-tracer wiring, and
  evaluator==tracer wiring.
- **H5 VERIFY honesty.** The mlflow row-error mapping guards column presence and carries
  `# VERIFY(...)`; no invented column names beyond that; no other vendor plugin touched.
- **H6/H7 blast radius + gates.** `git diff --stat` inside allowed paths; schema/capabilities/
  ci-adapters zero diff; guard untouched; ruff/mypy/pytest/standalone/make check/make contract
  tails; mutation evidence for all four C6 drills; `presets.py` untouched.
- **Scope honesty.** No ADR relitigation (D12 is decided), no docs edits, no config keys, no
  gate.py changes, no golden changes.

End with the gate decision in exactly this shape:

```text
## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
```
