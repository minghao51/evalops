# WP3.2a packet — VERIFY ledger: the beta registry, the run-name bug, and the logfire read path

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **503 offline tests, 13 contract tests**.
Read `docs/IMPLEMENTATION_MAP.md`'s 3.2 row (done-when: "zero unresolved VERIFY: each is an asserted
test or a documented `beta` with a user-visible hint"), `docs/STATE.md` compromise 18, then
`src/evalops/run.py` (the `_evaluate` artifact loop + the `evaluator.evaluate(...)` call),
`src/evalops/plugins/logfire.py` (whole file), `src/evalops/cli.py` (the `doctor` dispatch),
`tests/unit/test_cli_commands.py` + `tests/unit/test_parity.py` (doctor and fake-plugin patterns),
`tests/fixtures/parity_vendor_app.py` (the recording fake evaluator), and the ~50 `VERIFY` markers
(`grep -rn "VERIFY" src/evalops/plugins src/evalops/scorers`).

**This is slice A of three.** 3.2 is split by *verification environment*:
- **3.2a (this WP)** — the mechanism (a beta registry + a guard that makes "unresolved VERIFY"
  impossible), the `run.py` run-name bug, the logfire read path, and the beta entries for surfaces
  that need credentials we do not have (langsmith, litellm, the native judge adapters).
- **3.2b** — mlflow plugin resolution against the live docker stack (failure column `state`,
  expectation-derived phantom scores, `row_key`, `write()` mutation, `read()` order).
- **3.2c** — langfuse plugin resolution against the live Langfuse v4 project (`api.trace.get` →
  `api.observations.get_many`, dropped failing items, deprecated `dataset_run_url`).

## Environment

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 503 passed, 13 deselected
make check
```

Offline only: no docker, no network, no vendor SDK into `.venv`. Logfire is exercised with a fake
SDK module (`sys.modules` injection, the `test_native_judges.py` / `test_litellm_judge.py` style).

## Goal

Three things, in this order of importance:

1. **A real bug**: `_evaluate` names every vendor-evaluator run `"evalops/datasets.lock.json"`.
   The artifact-staging loop `for name, body in (...)` rebinds `name`, and `evaluator.evaluate(...,
   run_name=name)` is called afterwards (`run.py:578-586`, `run.py:606`). Server-side evidence from
   WP3.1b: every langfuse experiment is listed under that name; mlflow is affected whenever the
   ledger is not mlflow.
2. **The logfire read path** cannot work against the current SDK: `query_json_rows(sql=...)` is
   missing the now-required keyword-only `min_timestamp` (a `TypeError` today), the `httpx` extra
   the query client needs is undocumented, and the span name is an f-string whose interpolation
   depends on source introspection (observed half-literal in a site-packages import).
3. **"Zero unresolved VERIFY" becomes mechanically enforced** — not a promise. A marker is either
   deleted (because a test asserts the behavior) or converted to `VERIFY-BETA(<id>)` with an entry
   in a single registry that surfaces a user-visible hint through `evalops doctor`.

### A. The run-name bug (`src/evalops/run.py`)

- **A1** Capture the run name **before** the artifact loop (e.g. `run_name = name` right after the
  `name = f"{stage}-{version}-{dataset}-r{i + 1}"` assignment, or rename the loop variable to
  `artifact_name` — prefer renaming the loop variable, so no shadowing can recur) and pass that
  captured value as `run_name=` to `evaluator.evaluate`.
- **A2** Test: the recording fake evaluator (`tests/fixtures/parity_vendor_app.py`'s `STATE`) gains
  a `run_names` list; a unit test runs a real `_evaluate` through it and asserts the recorded name
  equals the run's ledger name (`dev-<app_version>-<dataset>-r1` shape), not an artifact path. A
  second assertion pins the ledger run name and the evaluator-visible name **identical** for a
  multi-repeat run (`-r1`, `-r2` distinct).

### B. The beta registry (`src/evalops/beta.py`, new)

- **B1** One frozen dataclass entry: `BetaSurface(id, area, summary, reason, hint)` —
  `id` is the marker key used in code (`VERIFY-BETA(<id>)`), `area` is the vendor/role
  (`langsmith`, `litellm`, `native-judges`, …), `summary` is one line of what is unverified,
  `reason` is why it cannot be verified in this phase (no credentials / server-side execution needs
  an LLM key / vendor behavior unobserved), `hint` is the **user-visible action** (e.g.
  "set LANGSMITH_API_KEY and run `make contract` with the langsmith extra to verify").
- **B2** `BETA_SURFACES: tuple[BetaSurface, ...]` — the single source of truth, sorted by id.
  Initial entries (each maps to existing markers; do not invent surfaces):
  `langsmith-pull-prompt-commit`, `langsmith-run-results`, `litellm-completion-shape`,
  `native-mlflow-make-judge`, `native-langfuse-managed-evaluator`, `native-openevals`,
  `native-pydantic-evals`, `settings-langfuse`, `settings-pydantic-evals`.
- **B3** `payload()` → `[{"id", "area", "summary", "reason", "hint"}]` (sorted, JSON-safe);
  `write_human(stream)` → one line per surface: `<id>  [<area>]  <summary> — <hint>`.
- **B4** `evalops doctor` gains `data["beta"] = beta.payload()` (always present, additive).
  Human mode prints a `beta (unverified surfaces):` block after the roles. This **changes the
  `doctor.json` golden once** — regenerate it deliberately with `EVALOPS_UPDATE_GOLDENS=1` and say
  so in the report; it is this WP's only golden churn.

### C. Marker conversion + the guard (`tests/unit/test_verify_markers.py`, new)

- **C1** In `src/evalops/plugins/*.py` and `src/evalops/scorers/*.py`, every `VERIFY` marker becomes
  exactly one of:
  - **deleted** — only where this WP actually resolves it (logfire: see D; nothing else in 3.2a);
  - **`VERIFY-BETA(<id>)`** — matching a `BETA_SURFACES` entry, keeping the existing explanatory
    text after it (marker conversion only: **no logic changes** in these files).
  Do **not** touch the trace-dialect markers (`traces/**`): they belong to phase 5 and are out of
  the guard's scope (say so in the guard's docstring).
- **C2** The guard test scans `src/evalops/plugins/*.py` + `src/evalops/scorers/*.py` for the token
  `VERIFY` and asserts:
  - every occurrence is `VERIFY-BETA(<id>)` with a registered id (a bare `VERIFY` fails);
  - every registered id has at least one marker (a stale registry entry fails);
  - every registry entry's `hint` is non-empty (a hint-less beta fails).
  The failure messages must name the file:line and the offending id — this test is the phase-3
  exit criterion made executable.
- **C3** A test that `doctor --json` carries `data.beta` with the registry payload and that the
  human rendering contains every `hint`.

### D. The logfire read path (`src/evalops/plugins/logfire.py`)

- **D1** `usage()` passes `min_timestamp=` — a `datetime` window derived from a new option
  `usage_lookback_hours` (default `24`), computed as `datetime.now(timezone.utc) - timedelta(hours=…)`
  per call. Keep the retry loop and the `usage_retries` / `usage_retry_seconds` options.
- **D2** The span name becomes the constant `"evalops.row"` with `row_id` / `evalops_run_id` as
  attributes (no f-string: the introspection-dependent interpolation is gone, and the name matches
  the other tracers, which the contract tests already assert for langfuse).
- **D3** Docstring: state that the query client needs `httpx` (and the `logfire` extra does not pull
  it), so a user enabling `read_token` installs `httpx` too; `doctor()`'s `usage_lookup` stays.
- **D4** The SQL is unchanged (it is `VERIFY-BETA(logfire-query-sql)`-eligible only if you cannot
  assert it — prefer asserting it: the fake-SDK test pins the exact SQL string and the
  `min_timestamp` kwarg, which resolves the marker by test; delete the marker and reference the
  test in the docstring).

### E. Falsifiable done-when

- **H1** `run_name` reaching the evaluator equals the ledger run name for vendor evaluators; a
  multi-repeat run gets distinct `-r1`/`-r2` names; the artifact loop no longer shadows it
  (mutation: restore the shadowing → the new test red).
- **H2** `beta.py` is the single registry; `doctor --json` exposes `data.beta` (sorted, with hints);
  human doctor prints every hint; `doctor.json` regenerated **once** and its diff is exactly the new
  `beta` key.
- **H3** No bare `VERIFY` remains in `plugins/` + `scorers/`; every `VERIFY-BETA(<id>)` resolves to a
  registry entry; no stale entries; hints non-empty. Mutations: (i) add a bare `VERIFY` → red;
  (ii) register an id with no marker → red; (iii) drop a hint → red.
- **H4** logfire `usage()` calls `query_json_rows` with `min_timestamp` (fake SDK asserts the kwarg
  and the SQL); the span name is `"evalops.row"` with the `row_id` attribute; the docstring names
  `httpx`.
- **H5** Blast radius: `git status --short` limited to `src/evalops/{run,beta,cli}.py`,
  `src/evalops/plugins/logfire.py`, the marker-conversion edits in `plugins/`+`scorers/`,
  `tests/unit/test_verify_markers.py`, `tests/fixtures/parity_vendor_app.py`,
  `tests/fixtures/envelope/doctor.json`, and the logfire/run-name test modules; **no** schema,
  capability or ci-adapter diff (`make schema`/`make capabilities`/`make ci-adapters` zero).
- **H6** Gates: `make check` green (503 → new total), standalone green, `make contract` skips
  offline (13 deselected), vendor-import guard untouched and green.

## Allowed paths

`src/evalops/run.py`, `src/evalops/beta.py` (new), `src/evalops/cli.py` (doctor surface only),
`src/evalops/plugins/logfire.py`, marker-only edits in `src/evalops/plugins/{mlflow,langfuse,langsmith}.py`
and `src/evalops/scorers/{native,litellm_judge}.py`, `tests/unit/test_verify_markers.py` (new),
`tests/unit/test_logfire_plugin.py` (new), `tests/unit/test_run_name.py` (new, or fold into
`test_parity.py`), `tests/fixtures/parity_vendor_app.py` (the `run_names` recorder),
`tests/fixtures/envelope/doctor.json` (regenerated once).

## Forbidden

Behavior changes in `plugins/{mlflow,langfuse,langsmith}.py` and `scorers/**` (marker conversion
only — 3.2b/3.2c own the logic), `traces/**` markers, `docs/**` (the orchestrator documents the beta
registry in CONTRACT/PLUGINS), `config/**` (no schema change), `presets.py`, `gate.py`, `cost.py`,
`envelope.py`, `scoring.py`, `tests/contract/**` (3.2b/3.2c update those), any golden other than
`doctor.json`, deleting/skipping/loosening tests, `git commit`/`push`/branch, installing anything
into `.venv`, any network call.

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
EVALOPS_UPDATE_GOLDENS=1 .venv/bin/python -m pytest -q tests/unit/test_envelope.py && git diff --stat tests/fixtures/envelope/
.venv/bin/evalops --json doctor | python3 -c "import json,sys; print(len(json.load(sys.stdin)['data']['beta']), 'beta surfaces')"
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Surface
<the registry shape; the guard's three assertions; the run-name fix; logfire changes>

## Marker ledger
<count before/after, per file: deleted vs VERIFY-BETA(id)>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements incl. the H3 mutations and the doctor.json diff>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3.2a (VERIFY ledger mechanism + run-name bug + logfire read path) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. Packet: `docs/packets/WP3.2a.md`.

- **H1 the bug.** Verify the fix kills the class, not the instance: no variable in `_evaluate`
  shadows `name` across the artifact loop and the evaluator call; the recorded evaluator name equals
  the ledger name for a multi-repeat run. Reproduce the original failure on a /tmp copy (restore the
  shadowing) and confirm the new test is red.
- **H2 registry + doctor.** `beta.py` is the only source; `doctor --json` carries sorted
  `data.beta` with non-empty hints; the human rendering prints them; `doctor.json`'s diff is exactly
  the added key (no other envelope churn).
- **H3 guard teeth.** This is the phase-3 exit criterion — try hard to defeat it: add a bare
  `VERIFY` in a plugin, register an id with no marker, drop a hint, put a marker in `traces/`
  (must be ignored), use an id with different case, split the marker across lines. Every case must
  fail with a file:line + id message, or be provably out of scope.
- **H4 logfire.** Fake-SDK test pins `query_json_rows(sql=..., min_timestamp=...)`, the SQL text,
  the span name `"evalops.row"` + `row_id` attribute, and that no f-string remains; docstring names
  `httpx`. Check the lookback default is sane and the retry loop survived.
- **H5 marker conversion honesty.** Diff the plugin/scorer files: only comments changed, zero logic
  (no `if`, no signature, no call-shape edit). Any logic change here is a BLOCK — 3.2b/3.2c own it.
- **H6 blast radius + gates.** `git diff --stat` inside the allowed paths; schema/capabilities/
  ci-adapters zero diff; guard test untouched; ruff/mypy/pytest/standalone/make check/make contract
  tails; the four H3 mutations reproduced.

End with the gate decision in exactly this shape:

```text
## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
```
