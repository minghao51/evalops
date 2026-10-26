# WP3b.3 packet — LiteLLM rubric fallback, strict parsing, per-call cost, spec→scorer wiring

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **400 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §3.0 (lazy import + exit 6), §6 (judge pinning — the normative rules),
`src/evalops/judges.py` (the `JudgeSpec`), `src/evalops/scorers/native.py` (the WP3b.2 adapter
registry and the lazy-import/exit-6 pattern to copy), `src/evalops/scorers/judges.py`
(`spec_judge` — the wrapper layer this WP extends), `src/evalops/scoring.py` (`to_float`,
`call_scorer`), `src/evalops/usage.py` (`report_usage`/`collect` — the per-call cost channel),
`src/evalops/cost.py` (`estimate`, `judge_cost_per_call`, `measure`), `src/evalops/run.py`
(`_build_scorers`, `plan`, `_evaluate` — where the wiring lands), `src/evalops/registry.py`
(`cfg.plugins[role]` — a plugin *name* without instantiating it), and
`tests/unit/test_native_judges.py` (the fake-SDK test pattern to copy).
Packet template: `docs/packets/WP3b.2.md` (newest).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 400 tests
.venv/bin/python tests/run_without_pytest.py   # use the venv interpreter
make check
```

No network, no docker, **no vendor SDK installed** — `litellm` included: the fallback is tested
against a **fake `sys.modules` module** (same pattern as WP3b.2: install inside the test with
`monkeypatch.setitem`, restore after; the doctor pins import vendor modules for real in the same
process). The ImportError path is tested for real (the venv genuinely lacks `litellm`).

## Goal

Phase 3b is native-first, but a fallback is part of the design ("the evalops rubric judge over
LiteLLM is a fallback, not the default" — map decision; G3b: "fallback **only where declared**").
WP3b.2 made one native adapter per evaluator; WP3b.3 delivers the evalops-owned **LiteLLM rubric
fallback** with **strict parsing**, attributes its **per-call cost**, wires a declared `JudgeSpec`
to a real scorer in `plan`/`evaluate`/`calibrate`, and adds the `litellm` optional dep — the first
`pyproject.toml` change since phase 2.

This WP does **not** enforce pins at the gate, record the resolved model per verdict, or add the
nightly server-side settings check — all **3b.5**. Pairwise/panel wrappers are **3b.4**.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. The executor declaration (`judges.<name>.judge`)

- **A1** `JudgeRule` gains one optional key: `judge: str | None`. Closed values: absent/`null` and
  `"native"` mean the evaluator's native adapter (WP3b.2 registry); `"litellm"` means the evalops
  rubric fallback (§B). Anything else is exit 2 naming `judges.<name>.judge` (config model, at the
  exact key). `judge` is **not** in `SPEC_KEYS` (WP3b.1's discriminator is unchanged): a rule with
  only thresholds + `judge: litellm` still declares **no** spec and stays a no-op key. Only a
  *declared spec* can select an executor.
- **A2** `JudgeSpec` carries the field (`judge: str = "native"`), validated in `validate()` against
  the same closed set (exit 2, dotted path) for direct API use; `from_rule` reads it;
  `payload()` gains `"judge"` (additive — the `judges.json` golden is regenerated once).
- **A3** `pyproject.toml` gains exactly `litellm = ["litellm>=1.40"]` in
  `[project.optional-dependencies]`. `PLUGIN_EXTRAS` is unchanged (no preset or plugin references
  `litellm`; the `test_presets` subset pins must stay green untouched). `make schema` regenerates
  `docs/evalops.schema.json` (the one new property under `judges.*` — expected diff, zero drift
  after commit).

### B. The fallback (`src/evalops/scorers/litellm_judge.py`, new core module)

- **B1** `litellm_judge(spec) -> neutral scorer`: `scorer(spec.name, llm=True)`-decorated callable
  `(*, inputs, outputs, expectations, trace=None) -> float`, exactly the WP3b.2 return contract.
  The factory imports `litellm` **inside the function body** (the guard-exempt lazy pattern;
  `litellm` is already in the guard's `VENDOR_ROOTS`) and translates `ImportError` into
  `CapabilityError("litellm", "rubric judge '<name>'", hint="pip install litellm")` → exit 6,
  chained from the ImportError. Copy the WP3b.2 shape; `# VERIFY(...)`-mark every litellm kwarg or
  response shape the installed SDK has not proven (same posture as the adapters — phase 3 resolves).
- **B2** The call passes the **full spec**: `model=spec.model` (never a literal), the rubric in the
  system message together with the scale declaration and a JSON output contract
  (`{"score": <number>}`, optional `"reason"`), the row payload (inputs/outputs/expectations,
  deterministically rendered, truncated at a named constant) in the user message, and
  `temperature`/`max_tokens`/`seed` from `spec.sampling` (`seed` omitted when `None`) plus
  `response_format={"type": "json_object"}` (JSON mode). The prompt template is a module constant
  with a version comment; `prompt_version` stays the user-declared spec field (recorded, not
  resolved — resolution/enforcement is 3b.5).
- **B3** Strict parsing — every failure is a raised `EvalOpsError(..., EXIT_RUNTIME)` naming the
  judge (`judge '<name>': ...`) with at most ~200 chars of raw output as evidence; the row loop's
  existing handler records it in `r.error` and the score is absent. Never `0.0`, never `None`,
  never a silent skip:
  - `choices[0].message.content` missing/empty → error;
  - `json.loads` fails → error;
  - no `"score"` key → error;
  - `"score"` not a JSON number (bools and numeric **strings** are not numbers) → error;
  - score outside `[scale.min, scale.max]` → **clamped** to the nearest bound (the scale clamp),
    in-range values pass through unchanged, binarization stays calibration's business (`threshold`),
    never the scorer's.
- **B4** Per-call cost: after a successful completion, read the response's token usage and call
  `report_usage(input_tokens=..., output_tokens=...)` (`from ..usage import report_usage`; the core
  evaluator's per-row `collect()` attributes it to `row.usage`, and `cost.measure` prefers reported
  usage). Missing/`None` usage on the response is skipped silently — evalops never invents tokens.
  This is the "cost attributed per judge call" deliverable.

### C. Resolution in the wrapper layer (`src/evalops/scorers/judges.py`)

- **C1** `spec_judge(spec, *, evaluator)` dispatches on the spec: `judge == "litellm"` →
  `litellm_judge(spec)` (the fallback is evaluator-independent); otherwise `native_judge` exactly
  as in WP3b.2 (unknown evaluator / missing SDK → exit 6 unchanged). `spec_judge` stays the single
  entry point; `span_judge`/`trajectory_judge` remain untouched.

### D. Wiring (`src/evalops/run.py` — the only behavior change)

- **D1** In `_evaluate`, after `scorers = _build_scorers(cfg)` and before the `scorer_filter`, append
  one scorer per declared spec (`judges.load(cfg)`, sorted by name):
  `spec_judge(spec, evaluator=<evaluator plugin name>)`. The evaluator name comes from
  `evaluator_plugin.name` (already instantiated in `_evaluate`). **Name collision** — the app hook
  already provides a scorer with `evalops_name == spec.name` — is exit **2** naming the name and
  both sources: ambiguous ownership is refused, never silently replaced or duplicated. Ordering
  honesty: spec *validity* is still exit 2 at `judges.load` before any estimate (WP3b.1); executor
  *SDK availability* surfaces at scorer build (exit 6) after the estimate/dry-run but **before**
  any budget reservation or ledger row — the same failure class as an app hook raising today.
- **D2** `plan` and `_evaluate` count the declared judge calls in the estimate:
  `n_llm += <number of declared specs whose executor makes client-side model calls>`. Client-side
  means: the litellm fallback, or a native adapter whose `NativeJudge` is not `server_side`.
  `NativeJudge` gains `server_side: bool = False` (`langfuse-managed` is `True` — server-side
  execution, no local tokens); nothing else in the registry or the capability matrix changes. In
  `plan`, resolve the evaluator **name** from `cfg.plugins["evaluator"]` (a config read; do not
  instantiate a plugin plan did not instantiate before). Estimate changes are therefore visible
  only for projects with declared specs — every existing test is unaffected.
- **D3** `calibrate.py` changes nothing: it filters `_build_scorers` output by judge-rule names, so
  a declared spec now yields real `pairs` (agreement/kappa/MAE) where before it could only report
  "labeled+scored pairs: false". Pin that with a fake-SDK test.

### E. Tests (`tests/unit/test_litellm_judge.py`, new; additions elsewhere as named)

- **E1** Fake `litellm` module (module-level `completion` recording its kwargs; response object
  with `choices[0].message.content` and `usage.prompt_tokens`/`completion_tokens`), installed with
  `monkeypatch.setitem(sys.modules, ...)` inside each test, never leaking (the doctor pins import
  vendor modules for real).
- **E2** Strict-parse matrix (call the scorer directly): valid `{"score": 0.83}` → `0.83`;
  invalid JSON → `EvalOpsError` naming the judge with a ≤200-char snippet; missing `score` →
  error; `true`/`"0.9"` as score → error; `1.7` on a 0..1 scale → clamped `1.0`; `-2` → `0.0`;
  in-range unchanged; `scale` variants (ordinal 1..5) clamp to their own bounds.
- **E3** Vendor-call shape: `model == spec.model` (a second spec with a different model produces
  that exact string — no literals), temperature/max_tokens pass through, `seed` omitted when
  `None`, `response_format` JSON mode, rubric present in the messages, **rubric text never in any
  error message** (only in the request).
- **E4** Real ImportError (no fake; `litellm` genuinely absent): `CapabilityError`, code 6, hint
  `pip install litellm`, chained from `ImportError`.
- **E5** Cost attribution: through a real `evaluate` on a temp project (fixture from
  `tests/unit/_projects.py`, core evaluator, fake litellm) — the judge's tokens land in
  `row.usage` (added to any app-reported usage) and `cost.measure` counts them.
- **E6** Wiring: (a) declared spec, no `judge` key → the native adapter runs (fake `pydantic_evals`
  on the core evaluator); (b) `judge: litellm` → the fallback runs; (c) threshold-only rules add no
  scorer (eval adds nothing, byte-identical to today); (d) app scorer + declared spec with the same
  name → exit 2 before any ledger row; (e) unknown `judge` value → exit 2 at `judges.load` in
  `plan`/`evaluate`/`calibrate` and in `evalops judges` (add to `test_judge_spec.py`'s matrix).
- **E7** Estimate: `plan` on a declared-spec project bumps `llm_calls`/`judge_usd` by one
  client-side call per row per spec; a `langfuse-managed` spec (server-side) does not bump; a
  threshold-only rule does not bump; a project without declared specs is unchanged.
- **E8** Calibration: declared spec + fake SDK → `evalops calibrate` reports agreement/kappa/MAE
  for the judge (pairs non-empty) — the D3 pin.
- **E9** Goldens/schema: `judges.json` regenerated additively (the `judge` key per spec);
  `docs/evalops.schema.json` gains exactly the `judges.*.judge` property; `make capabilities` /
  `make ci-adapters` zero drift; no other golden churn.

## Falsifiable done-when

- **H1** `judges.<name>.judge` exists with the closed value set (absent/`native`/`litellm`),
  validated in the config model at the exact key and in `JudgeSpec.validate()` at the dotted path,
  both exit 2; `judge` is not a `SPEC_KEYS` member (threshold-only rules unchanged, `unpinned`
  worklist unchanged); `payload()`/`evalops judges --json` expose `judge`; the schema diff is
  exactly the one property.
- **H2** `litellm_judge` returns a `call_scorer`-compatible scorer named `spec.name` (`llm=True`);
  the full spec reaches the litellm call (model, sampling, JSON mode, rubric); every unverified
  litellm detail is `# VERIFY`-marked.
- **H3** Every strict-parse failure raises (judge named, short snippet) and never produces `0.0` or
  `None`; out-of-range scores clamp to the scale; in-range scores pass through; a bool or string
  score is an error.
- **H4** Missing `litellm` → exit 6 `pip install litellm`, chained from ImportError, no traceback,
  no rubric text in the message.
- **H5** The fallback's token usage is attributed per call: visible in `row.usage` and counted by
  `cost.measure` (fake-SDK evaluate on a temp project).
- **H6** `spec_judge` dispatches (`native` → registry, `litellm` → fallback, unknown evaluator
  still exit 6); a declared spec reaches `evaluate` as a real metric (both executors, fake SDKs);
  threshold-only projects are byte-identical; the collision case is exit 2 before any ledger row.
- **H7** `plan`/`_evaluate` estimates include one client-side call per row per declared spec;
  `langfuse-managed` and threshold-only rules do not bump; `NativeJudge.server_side` exists with
  only `langfuse-managed` true; projects without declared specs estimate exactly as before.
- **H8** Gates: `make check` green (400 → new total), standalone runner green, `make contract`
  skips, `make schema` diff = the one property then zero drift, `make capabilities`/`make
  ci-adapters` zero diff, the only golden change is `judges.json` (additive), core stays
  vendor-free, and `git status --short` lists only: `src/evalops/scorers/litellm_judge.py` (new),
  `src/evalops/scorers/judges.py`, `src/evalops/scorers/native.py` (`server_side` only),
  `src/evalops/judges.py` (`judge` field + payload + validation),
  `src/evalops/config/models.py` (`JudgeRule.judge`), `src/evalops/run.py` (D1/D2 only),
  `pyproject.toml` (the `litellm` extra only), `docs/evalops.schema.json` (regenerated),
  `docs/CAPABILITIES.md` (only if regeneration actually changes it), `tests/unit/test_litellm_judge.py`
  (new), `tests/unit/test_judge_spec.py` (the `judge`-key cases), `tests/unit/test_native_judges.py`
  (the `server_side` pin), `tests/unit/test_no_vendor_imports.py` (one litellm probe),
  `tests/fixtures/envelope/judges.json` (regenerated). Nothing else.
- **H9** No behavior change outside the WP: `gate.py`, `calibrate.py`, `pipeline.py`, `plugins/**`,
  `traces/**` untouched; projects without declared specs get byte-identical plan/eval/calibrate/
  gate envelopes (existing tests green and unmodified except the named additive cases).
- **H10** Mutations (reproduce on /tmp copies, report each red test): (i) the fallback returns
  `0.0` on a JSON parse failure → the strict-parse test red; (ii) the clamp removed → red;
  (iii) the collision check dropped (silent replace or duplicate) → red; (iv) the `n_llm` bump
  removed from `plan` → red; (v) an unknown `judge` value accepted (or `judge` added to
  `SPEC_KEYS`) → the config/golden test red; (vi) `import litellm` hoisted to module level in
  `scorers/litellm_judge.py` → the guard test red.

## Allowed paths

`src/evalops/scorers/litellm_judge.py` (new), `src/evalops/scorers/judges.py` (the dispatch only),
`src/evalops/scorers/native.py` (`server_side` only), `src/evalops/judges.py` (`judge` field +
validation + payload), `src/evalops/config/models.py` (`JudgeRule.judge` + closed-set validation),
`src/evalops/run.py` (D1/D2 only), `src/evalops/plugins/core.py` (**orchestrator adjudication,
narrow:** the `score()` scoring path opens its own `usage.collect()` per scorer call — collectors
must be opened *inside* the pool worker, contextvars do not cross threads — and merges any
reported usage additively into `row.usage` (tokens sum; `cost_usd` sums when either side has one;
never replaces app-reported usage; the tracer backfill's `is None` guard keeps precedence). Both
scoring paths are covered by the same closure (`run_row` and the `stored_rows` map). No other
core.py behavior changes: retries, error sentinels, breaker, trace capture untouched),
`pyproject.toml` (the `litellm` extra only),
`docs/evalops.schema.json` (regenerated), `docs/CAPABILITIES.md` (regenerated only if needed),
`tests/unit/test_litellm_judge.py` (new), `tests/unit/test_judge_spec.py` (`judge`-key cases),
`tests/unit/test_native_judges.py` (`server_side` pin), `tests/unit/test_no_vendor_imports.py`
(one probe), `tests/fixtures/envelope/judges.json` (regenerated),
`tests/fixtures/envelope/config_schema.json` (regenerated — the same document as the schema;
WP3b.1 precedent), and a pin that the judge's own tokens reach `row.usage`/`cost.measure`
end-to-end (H5's other half must not stay partial).

## Forbidden

`src/evalops/gate.py` (pin enforcement is 3b.5), `calibrate.py`, `pipeline.py`, `cost.py`,
`usage.py`, `plugins/**` except the adjudicated `core.py` scoring-collector change, `traces/**`,
`scorers/trajectory.py`,
`scorers/native.py` beyond the flag, `Makefile`, `ci/**`, `evalops.yaml`, `datasets/**`,
`docs/**` except the two generated files, any other envelope golden, new dependencies beyond the
one extra, deleting/skipping/loosening tests, `git commit`/`push`/branch, installing any vendor
SDK (litellm included) into `.venv`.

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
make schema && git diff --stat docs/evalops.schema.json
make capabilities && git status --short docs/CAPABILITIES.md
make ci-adapters && git status --short ci/
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Fallback + wiring surface
<the executor key; the litellm call shape (VERIFY count); the strict-parse matrix; the wiring
points and their exit codes; the estimate change>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the H10 mutation evidence>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3b.3 (LiteLLM rubric fallback + wiring) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. The packet is `docs/packets/WP3b.3.md` — read it,
then `docs/CONTRACT.md` §6, `src/evalops/scorers/litellm_judge.py`, the `spec_judge` dispatch, the
`judge` key in `judges.py`/`config/models.py`, the D1/D2 wiring in `run.py`, and the new tests.

- **H3 strictness hunt.** Drive the parse matrix yourself against the fake: invalid JSON, missing
  key, bool score, numeric-string score, out-of-range both sides, empty content. Each must raise
  with the judge named and no `0.0`/`None` escape; in-range passes through; clamps hit the declared
  scale's own bounds. A mutation that swaps clamp for rejection (or vice versa) must be caught by
  the tests — try it.
- **H5 cost honesty.** The usage attribution must go through `report_usage` inside the row (check
  the row's `usage` through a real `evaluate`, and that `cost.measure` counts it); a response
  without usage must not invent tokens. Verify the estimate bump (H7) is per-row, per
  client-side spec, and that `langfuse-managed` (server_side) and threshold-only rules do not bump.
- **H6 wiring blast radius.** Threshold-only projects must be byte-identical: run the existing
  plan/eval/calibrate tests and confirm no golden churn beyond `judges.json`. The collision case
  must be exit 2 **before** any ledger row. Check the ordering claim: spec validity (exit 2) at
  `judges.load` before the estimate; SDK availability (exit 6) at scorer build before reservation.
- **H1 config surface.** `judge` accepts exactly absent/`native`/`litellm`; unknown → exit 2 at the
  dotted key in the config model AND in `JudgeSpec.validate()`; `judge` alone does not declare a
  spec (a threshold-only rule with `judge: litellm` stays out of `specs()`/`unpinned` exactly as
  before); the schema diff is exactly one property; the `judges.json` golden diff is exactly the
  new key.
- **H4 real ImportError.** No fake in `sys.modules`: exit 6, `pip install litellm`, chained cause,
  no traceback, no rubric text. Then run the doctor pins after the fake-SDK tests in the same
  session — no fake may leak.
- **H10 mutations.** Reproduce all six on /tmp copies; name the red test each.
- **H8/H9 blast radius.** `git diff --stat` limited to allowed paths; `gate.py`/`calibrate.py`/
  `pipeline.py`/`plugins/**` untouched; pyproject gains exactly one extra; no test weakened;
  standalone runner green; core vendor-free (the guard test green, `litellm` probe present).
- **Scope honesty.** No gate-time pin enforcement, no resolved-model recording, no nightly settings
  check, no pairwise/panel (3b.4/3b.5). Grep for `resolved`, `gate`, `pairwise` surprises. The
  fallback must not be reachable except through a declared spec (`judge: litellm` or the dispatch).

End with the gate decision in exactly this shape:

## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
