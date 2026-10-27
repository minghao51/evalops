# WP3b.5 packet — pin enforcement + resolved model per verdict + nightly settings check + calibration as gate input

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **454 tests** (+1 contract, deselected).
Read `docs/IMPLEMENTATION_MAP.md`'s phase-3b row (this WP's deliverable and done-when: "unpinned
judge → exit 1 with reason; drift in judge model fails nightly; calibration blocks promotion"),
`docs/CONTRACT.md` §6 (the enforcement bullets are this WP's done-when — they stop being "target:
WP3b.5" when you land), `docs/STATE.md` compromises 14 and 17, `src/evalops/gate.py` (`check`,
`run_gate` — where the gate refusal lands), `src/evalops/judges.py` (`is_pinned_model`, `specs`,
`load`, `payload` — the `unpinned` worklist), `src/evalops/run.py` (`plan`, `_evaluate`,
`_with_spec_judges`, `row_to_json`, `rows_from_artifact`), `src/evalops/scorers/judges.py`
(`spec_judge` — the single dispatch point), `src/evalops/calibrate.py` (what a calibration run
records: `judge.<name>.<stat>` metrics + the `evalops.calibration` tag), `src/evalops/pipeline.py`
(`_plan_nightly`), and `src/evalops/scorers/native.py` (the `ADAPTERS` registry, the lazy-import /
fake-SDK / `VERIFY` conventions).

**This is the last phase-3b WP: after it, G3b tags `v0.5.0`.**

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 454 tests
.venv/bin/python tests/run_without_pytest.py   # use the venv interpreter
make check
```

Offline rule: **no vendor SDK into `.venv`** — every server-side read is fake-SDK-tested (the
`test_native_judges.py` fake-module style: inject a stub module into `sys.modules`, assert what
evalops passes and how it normalizes). `LiteLLM`/native paths are already fake-tested; you add no
new dependency and no docker.

## Goal

CONTRACT §6's four enforcement bullets become code. Phase 3b made judges *representable* (specs,
native adapters, fallback, wrappers); this WP makes them **accountable**:

1. **Gate-time pin enforcement, every judge** — an unpinned judge fails the gate (exit 1) with a
   reason; it is never a warning.
2. **`judge_meta()` never overrides the config pin** — a disagreement is exit 2 at load.
3. **Resolved model + prompt version recorded per verdict** — a verdict without a resolved model is
   a contract violation.
4. **Nightly server-side judge settings check** — Langfuse project defaults, pydantic-evals
   defaults; drift fails nightly.
5. **Calibration as a gate input** — the gate consumes recorded calibration evidence; failing
   evidence blocks. (The pipeline-stage half is WP2.5 and stays as pinned.)

Deliberately **out of scope**: any new config key (the schema must not change: `make schema` zero
drift), any preset change, any change to `scorers/panel.py` / `litellm_judge.py` / the scorer
protocol, envelope shape changes beyond the surfaces named below, phase-3 VERIFY resolution beyond
the two new marked readers.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. Gate-time pin enforcement (`src/evalops/gate.py`)

- **A1** In `run_gate`, after `rules = cfg.section("gates")` is read, load `judges_rules =
  cfg.section("judges") or {}`. For **every** rule (sorted by name), `judges.is_pinned_model(rule.get("model"))`
  must be true. A rule that fails — model absent, empty, or an alias (`latest`, `*-latest`, `auto`,
  `default`, `project-default`, `default/*`, `*/*/default`) — appends one failed check:
  `{"check": f"judge pin: {name}", "passed": False, "reason": <the dotted key judges.<name>.model,
  the offending value, and the contract line "an unpinned judge fails the gate (exit 1)">`.
  The gate `passed` is ANDed with these checks like any other, so the command exits **1**.
- **A2** This covers *every* rule shape: declared specs (already exit 2 at load when unpinned — the
  gate must not assume load ran and still checks them), threshold-only rules, and model-only rules.
  A rule with a concrete pin produces **no** check (the check list stays quiet about healthy judges).
- **A3** No `judges:` section → no checks, and the envelope is byte-identical to today. This is the
  regression tripwire: the gate goldens (`gate_pass.json`, `gate_failure.json`) come from a project
  with no judges section and **must not change**.
- **A4** Applies to every `gate` invocation, including `--label drift` (the nightly drift gate):
  `run_gate` is the single enforcement point.
- **A5** The pin checks land in the existing `checks` list — **no new envelope `data` key**, so no
  gate golden churn and no CONTRACT §2 change.

### B. `judge_meta()` may add, never override (`src/evalops/judges.py` + call sites)

- **B1** New function in `judges.py` (fixed name `conflicts_with_meta(cfg, meta) -> None`): compares
  the run-level judge identity metadata against the declared specs. Only two meta keys are
  identity-bearing: `judge_model` and `judge_prompt_version` (the keys `fake_app.py`'s hook and
  CONTRACT §6 speak of; anything else in `meta` is app metadata and is never compared).
- **B2** Semantics: for each of the two keys **present** in `meta`, every declared spec
  (`judges.load(cfg)`) must carry the **equal** value for the corresponding field (`model` /
  `prompt_version`; exact string equality — a non-string or empty meta value equals nothing and is
  a conflict). Any disagreement raises `EvalOpsError(EXIT_USAGE)` naming `judges.<name>.model`
  (resp. `judges.<name>.prompt_version`), the config value and the meta value, with the reason that
  `judge_meta()` may add metadata but must never override the config pin. Specs are checked in
  sorted name order; `judge_model` before `judge_prompt_version`. Absent keys, zero declared specs,
  or full agreement → no-op.
- **B3** Call sites: `run.py::plan`, `run.py::_evaluate`, and `calibrate.py::calibrate` — each right
  after `meta = cfg.hook("judge_meta")()` (the load points; `evalops judges` stays a pure listing
  and must NOT call it). An exit here is pre-estimate and pre-ledger, same as `judges.load`.
- **B4** Rationale (goes in the module docstring, one line): with a declared spec the judge identity
  is config-owned and is what this WP records per verdict; a disagreeing run-level metadata value
  would contradict that record on the same run. Equal values are fine — metadata may *add*.

### C. Resolved model + prompt version recorded per verdict (`src/evalops/run.py`, `ports.py`, `scorers/judges.py`)

- **C1** `spec_judge` stamps its returned scorer with the spec identity:
  `scorer.evalops_judge = {"model": spec.model, "prompt_version": spec.prompt_version}` (a plain
  attribute on the function object `native_judge`/`litellm_judge` return). `spec_judge` is the
  single dispatch point, so both executors are covered without touching their modules.
- **C2** `RowResult` gains a trailing field `judge_models: dict[str, dict[str, str]] | None = None`
  (additive; `None` = v0 writer/reader). Positional constructors stay valid.
- **C3** `_evaluate` builds the identity map once from the already-loaded declared specs and, after
  scoring (covering both the native-scorer path and the `_post_score` trace-scorer path), stamps
  each row: `row.judge_models = {name: identity for every declared name whose score is in
  row.scores}`. Then the invariant, judge-named: every score key that names a declared judge must
  have its identity entry, or `evaluate` raises `EvalOpsError(EXIT_RUNTIME)` naming the judge —
  **a verdict without a resolved model is a contract violation, not a silent null**.
- **C4** `row_to_json` emits the field (it rides `asdict`); `rows_from_artifact` reads it
  additively (`d.get("judge_models")` — v0 lines read fine, as CONTRACT §5 requires). Re-scored
  `--judge-only` rows get re-stamped from the current specs, so a stale value can never survive.
- **C5** Run level: `_evaluate` also records `judge.<name>.model` and `judge.<name>.prompt_version`
  per declared spec in the run params (additive keys beside the existing `judge.*` metadata) —
  CONTRACT §6 says the identity is recorded "on the run".
- **C6** Envelope/golden discipline: the `eval` golden has no per-row payload and the `traces`
  golden renders explicit keys — both stay byte-identical. The only persisted surface that grows is
  `evalops/rows.jsonl` (additive field, exactly the WP1.5 `trace` precedent). The gate/plan/judges
  goldens are untouched by C.

### D. Calibration as a gate input (`src/evalops/gate.py`)

- **D1** In `run_gate`, for every judges rule carrying at least one of `min_agreement`, `min_kappa`,
  `max_mae` (non-null), the gate looks up the judge's latest calibration evidence in the ledger:
  runs tagged `evalops.kind: calibration` whose metrics carry any `judge.<name>.<stat>` key
  (exactly what `calibrate.py` records), latest by `(start_ms, run_id)`, deterministic.
- **D2** Evidence exists → the gate re-checks the **config's** limits against the run's recorded
  metrics (`judge.<name>.agreement >= min_agreement`, `judge.<name>.kappa >= min_kappa`,
  `judge.<name>.mae <= max_mae`; each configured limit emits at most one check, check name
  `judge calibration: <name>`). A limit that fails — or a latest run whose `evalops.calibration`
  tag is `failed`, or a run missing the metric a configured limit needs — is a failed check with
  detail `{"candidate": <measured or null>, "required": <limit>, "calibration_run": <run_id>}` and
  fails the gate (exit 1).
- **D3** No calibration run exists for the judge → **no check is emitted**. This coexists with the
  pinned WP2.5 decision (STATE compromise 14: enforcement triggers on calibration evidence that
  fails, never on a missing dataset; absence keeps the pipeline's calibrate step a skip). The gate
  consumes what exists; it does not demand what the pipeline deliberately skips.
- **D4** The check's detail names the consumed run (`calibration_run`) so an operator can see which
  evidence decided the gate.

### E. The `unpinned` worklist becomes exhaustive (`src/evalops/judges.py`)

- **E1** `payload()["unpinned"]` = the sorted names of **every** rule that fails
  `is_pinned_model(rule.get("model"))` — threshold-only rules, model-only rules, empty rules; the
  STATE compromise 17 line ("a rule with only `model` is neither a spec nor on the worklist") is
  replaced by this WP. Declared specs never appear (their pins were validated at load).
- **E2** `write_human`'s warning line names the pin requirement: one line per name, of the shape
  `warning: judges.<name> is unpinned; gate refuses it (exit 1) — set judges.<name>.model to a
  concrete model` (keep it one line; no rubric text anywhere).
- **E3** The module docstring's "deliberate WP3b.1 line" paragraph is rewritten to the WP3b.5
  posture (the worklist is the gate's refusal list; `is_pinned_model` is its single rule).
- **E4** `tests/unit/test_envelope.py`'s `JUDGES_CONFIG` fixture gains a third rule — model-only,
  e.g. `traced: {model: fake-judge-2024-11-20}` — so the golden proves the new semantics: before
  this WP it appears nowhere, after it, it is on `unpinned`. Regenerate `judges.json` **once** with
  `EVALOPS_UPDATE_GOLDENS=1` and say so in the report. This is the WP's only golden churn.

### F. Nightly server-side judge settings check (`scorers/native.py`, `judges.py` CLI, `pipeline.py`)

- **F1** Two settings readers, one per server surface CONTRACT §6 names, living beside the adapters
  in `scorers/native.py` with the same conventions (lazy SDK import inside the function body, fake
  SDK in tests, `VERIFY(...)` on the unverified vendor call shape, missing SDK → `CapabilityError`
  exit 6 naming the pip package):
  - `langfuse` → the project's default judge model (Langfuse project defaults);
  - `core` → the pydantic-evals default judge model (pydantic-evals defaults).
  Each reader returns the server-side default model string for comparison, or raises.
- **F2** New CLI flag `evalops judges --check-settings` (the plain listing without the flag is
  byte-identical to today): for every declared spec whose evaluator has a settings surface, read
  the server default and compare to `spec.model`. The envelope `data` shape (fixed):
  `{"evaluators": [ {"evaluator": <name>, "status": "ok"|"drift"|"no-settings-surface",
  "judges": [ {"name", "config_model", "server_default"} ] } ], "drifted": bool}` — evaluators
  sorted by name; `status: "no-settings-surface"` for evaluators whose readers don't exist yet
  (mlflow, langsmith — phase 3 resolves them; visible, not hidden). Exit **1** iff `drifted`
  (envelope code `GATE_FAILED`); exit 6 when a needed SDK is missing; otherwise 0.
- **F3** Pipeline wiring: `_plan_nightly` yields the step right after `baseline` and **before** the
  baseline checkout — `_ev("judge-settings", ["judges", "--check-settings"])` — so the check sees
  the working tree's config and credentials, and drift fails the nightly (the stage's exit
  propagation already does this; the step needs no undo). Dry-run lists it like any step.
- **F4** Tests inject fake SDK modules (the `test_native_judges.py` style); drift and agreement
  both proven; the `no-settings-surface` status pinned; the pipeline step proven in dry-run.

### G. Deliberate behavior changes — the ledger you must ship in your report

This WP **changes gate and load behavior** for judge-bearing configs. Every item below is
intentional; each needs a test that pins it, and every existing test you touch must keep its
original assertion target (the *cause* under test must not silently change):

1. Threshold-only configs (no model) now fail the gate with `judge pin: <name>` (A1). Known
   collisions to update by **adding a concrete pin** to the fixture config, keeping every
   assertion: `tests/unit/test_calibration_enforcement.py` (the skip test pins WP2.5's
   absence-is-a-skip decision — with a pin it still passes: calibrate stays skipped, gate stays
   green; the failing-agreement tests keep failing at the *calibrate* step);
   `tests/unit/test_pipeline.py` (`JUDGES` at line ~54, used ~line 250);
   `tests/integration/test_e2e_agent.py` (`judges.helpful` — the gate calls at steps 3–5 must stay
   exit 0/1 for their original reasons; pick the pin the fixture's `judge_meta` already claims so
   B does not fire).
2. A `judge_meta()` whose identity keys disagree with a declared spec is now exit 2 at load (B).
   Existing fixtures that use `fake_app:judge_meta` (`judge_model: "fake-judge@1"`) and declare
   specs must pin the same value — that is the fixed convention, not a workaround.
3. Calibration evidence that fails now fails the gate (D) — new unit test; no existing test runs a
   gate after a failed calibration today (verified: `test_e2e_agent.py` gates only before
   calibrating).
4. `unpinned` grows (E) — `test_judge_spec.py`'s worklist tests and the `judges.json` golden
   (regenerated once).
5. Configs with **no** `judges:` section are byte-identical everywhere: gate pass/fail, eval, plan,
   traces goldens must all be unchanged — this is the proof that the refusal is scoped.

## Tests (`tests/unit/test_judge_enforcement.py` new; `tests/unit/test_judge_settings.py` new; targeted edits elsewhere)

- **C1 (enforcement)** Gate pin matrix on temp projects (`tests/unit/_projects.py`): no judges
  section → gate byte-identical (assert the golden files did not move, plus exit 0); threshold-only
  without model → exit 1 with the `judge pin: <name>` check naming `judges.<name>.model`;
  `model: latest` / `default/openai/gpt-4` / empty → same; pinned threshold-only and pinned
  model-only rules → no pin check, exit unchanged. Pin checks present for `--label drift` too.
- **C2 (meta conflict)** `conflicts_with_meta`: equal values pass; `judge_model` disagreement →
  exit 2 naming `judges.<name>.model` with both values; `judge_prompt_version` disagreement → same
  at `.prompt_version`; two specs, one disagreeing → the sorted first fires; absent keys / no
  specs / non-identity metadata (e.g. `llm_judge_count`) → no-op. End-to-end: `plan` on a project
  whose hook disagrees → exit 2 envelope before any estimate; `evalops judges` on the same project
  still exits 0 (B3's listing exemption).
- **C3 (per-verdict identity)** Real `evaluate` on a temp project with one declared spec (native
  `core` fake path, as `test_native_judges.py` does): every row carrying the spec's score carries
  `judge_models[<name>] == {"model": spec.model, "prompt_version": spec.prompt_version}`; the run
  params carry `judge.<name>.model` / `judge.<name>.prompt_version`; `rows.jsonl` lines carry the
  field; `rows_from_artifact` round-trips it and accepts a v0 line without it (v0 fixture);
  mutation: drop the stamping → evaluate raises the judge-named contract violation (exit 5).
- **C4 (calibration input)** Temp project with thresholds + a recorded calibration run: passing
  evidence → check passes, gate green; failing evidence (`evalops.calibration: failed`, or a metric
  below the config limit, or a missing metric) → exit 1 with `judge calibration: <name>` naming
  measured/required/run; no evidence → no check, gate unchanged (the STATE 14 posture); two runs →
  the latest by `(start_ms, run_id)` wins (prove the tiebreak).
- **C5 (worklist)** `payload()` on rules: threshold-only, model-only unpinned, model-only pinned,
  declared spec, empty rule → the exhaustive sorted list; `write_human` renders the fixed warning
  line; the regenerated `judges.json` golden contains the model-only rule under `unpinned`.
- **C6 (settings check)** Fake-SDK: agreement → `drifted: false`, exit 0; drift → the finding names
  config vs server model, exit 1; missing SDK → exit 6 naming the package; mlflow/langsmith specs →
  `no-settings-surface`; no declared specs → `evaluators: []`, exit 0; plain `evalops judges`
  unchanged. Pipeline: nightly dry-run lists `judge-settings` after `baseline`; a real drift run
  fails the stage with the step's exit 1 (fake SDK through the CLI, as `test_pipeline.py` does for
  nested steps).
- **C7 (mutations, on /tmp copies; report each red test)**: (i) skip the pin check in `run_gate` →
  C1 red; (ii) drop `conflicts_with_meta` from `_evaluate` → C2 red; (iii) drop the C1/C3 stamping
  → the C3 contract-violation test red; (iv) restore the old threshold-only `unpinned` filter → C5
  red; (v) make drift report `drifted: false` → C6 red; (vi) emit the calibration check when no
  evidence exists → the C4 absence test red (this guards the STATE 14 posture from the other side).

## Falsifiable done-when

- **H1** Every rule without a concrete pin fails the gate (exit 1) with a reason naming
  `judges.<name>.model`; pinned rules and no-judges configs produce no new checks; the gate
  pass/fail goldens are byte-identical.
- **H2** A `judge_meta()` identity that disagrees with any declared spec is exit 2 at load at all
  three call sites, naming the dotted key and both values; `evalops judges` still exits 0; equal
  or absent values change nothing.
- **H3** Every declared-spec verdict carries `judge_models[<name>]` with the pinned model and
  prompt version in `rows.jsonl`, and the run params carry `judge.<name>.model` /
  `judge.<name>.prompt_version`; a verdict without identity is the judge-named exit-5 contract
  violation; v0 rows artifacts still read.
- **H4** The gate consumes the latest calibration evidence per judge and fails on failing evidence,
  naming measured/required/run; absence of evidence emits nothing (STATE 14 intact, test-pinned
  both ways).
- **H5** `unpinned` lists every unpinned rule (model-only included); the `judges.json` golden —
  the WP's only golden churn — regenerated once and diffed deliberately; `write_human` names the
  pin fix.
- **H6** `evalops judges --check-settings` implements F2 exactly (ok/drift/no-surface/exit 6),
  fake-SDK-tested offline; the nightly stage runs it before checkout and drift fails the stage.
- **H7** Blast radius: `git status --short` lists only the allowed paths below; `make schema`,
  `make capabilities`, `make ci-adapters` zero diff; no new dependency; the vendor-import guard
  untouched and green; no judges-section-less project changes behavior anywhere (goldens are the
  proof).
- **H8** Gates: `make check` green (454 → new total), standalone runner green, `make contract`
  skips offline.

## Allowed paths

`src/evalops/gate.py`, `src/evalops/judges.py`, `src/evalops/run.py`, `src/evalops/calibrate.py`,
`src/evalops/cli.py` (the `--check-settings` flag + dispatch only), `src/evalops/pipeline.py`
(the nightly step only), `src/evalops/ports.py` (the one additive `RowResult` field only),
`src/evalops/scorers/judges.py` (the C1 stamping only), `src/evalops/scorers/native.py` (the two
settings readers only),
`tests/unit/test_judge_enforcement.py` (new), `tests/unit/test_judge_settings.py` (new),
`tests/unit/test_gate_rules.py`, `tests/unit/test_judge_spec.py`, `tests/unit/test_pipeline.py`,
`tests/unit/test_calibration_enforcement.py`, `tests/unit/test_native_judges.py` (only if a fake
fixture is shared), `tests/unit/test_envelope.py` (the `JUDGES_CONFIG` extension + the one golden
regen), `tests/integration/test_e2e_agent.py` (the fixture pin only),
`tests/fixtures/envelope/judges.json` (regenerated once, E4).

## Forbidden

`src/evalops/config/**` (no schema change — `make schema` must be zero diff), `envelope.py`,
`scoring.py`, `scorers/panel.py`, `scorers/litellm_judge.py` (the C1 stamping lives in
`spec_judge`, not in the executor modules — if you believe one must change, stop and report a
blocker), `plugins/**`, `traces/**`, `presets.py`, `cost.py`, `usage.py`, `ci_adapters.py`,
`pyproject.toml`, `Makefile`, `ci/**`, `evalops.yaml`, `datasets/**`, `docs/**` (the orchestrator
owns the CONTRACT §6 / STATE / map updates in the docs commit), any golden other than
`judges.json`, deleting/skipping/loosening tests, `git commit`/`push`/branch, installing anything
into `.venv`, any real network or SDK call in tests.

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
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Enforcement surface
<gate pin checks; meta-conflict rule; per-verdict identity path; calibration-input lookup;
worklist definition; settings readers + flag + pipeline step>

## Behavior changes
<each deliberate change from section G, with the test that pins it and every existing test touched>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<10-14 falsifiable statements, incl. the C7 mutation evidence and the golden-diff stat>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3b.5 (pin enforcement + resolved model per verdict + nightly settings check + calibration as gate
input) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`. The packet is
`docs/packets/WP3b.5.md` — read it first, then `src/evalops/gate.py`, `judges.py`, `run.py`,
`calibrate.py`, `cli.py`, `pipeline.py`, `scorers/judges.py`, `scorers/native.py`, `ports.py`, and
the two new test files. This WP **changes gate and load behavior on purpose**; your job is to make
the deliberate changes exact and hunt for the unintended ones.

- **H1 gate pins.** Drive the matrix yourself: threshold-only, model-only, alias pins, empty rules,
  `--label drift`; a silent pass for any unpinned shape is a defect. Then the tripwire: confirm
  `gate_pass.json`/`gate_failure.json` (and eval/plan/traces) are byte-identical — a no-judges
  project must not change behavior at all. Try to sneak a pin check into a no-judges envelope.
- **H2 meta conflict.** The rule is: only `judge_model`/`judge_prompt_version` are identity-bearing;
  equal passes, disagreeing is exit 2 at the dotted key with both values, at plan/evaluate/calibrate
  but NOT at `evalops judges`. Verify the listing exemption and the sorted-first failure order;
  verify a fixture whose hook matches its pin stays green end-to-end.
- **H3 per-verdict identity.** Real `evaluate`: rows carry `judge_models`, run params carry
  `judge.<name>.model`/`.prompt_version`, `rows.jsonl` round-trips, v0 lines still read. Mutation:
  remove the stamping — evaluate must fail with the judge-named exit-5 contract violation, not a
  null. Check the additive-field discipline against CONTRACT §5 (the WP1.5 `trace` precedent) and
  that positional `RowResult` constructors still work.
- **H4 calibration input.** Failing evidence fails the gate naming measured/required/run; absence
  emits NOTHING (STATE compromise 14's pinned decision — prove both directions, including the C7-vi
  mutation); latest-by-(start_ms, run_id) tiebreak is deterministic; the gate re-checks the
  config's limits against the run's recorded metrics, not the run's own verdict.
- **H5 worklist.** `unpinned` is now every rule failing `is_pinned_model` (STATE compromise 17
  resolved); the regenerated `judges.json` is the ONLY golden that moved — run
  `git status --short tests/fixtures/envelope/` yourself and name anything else. Check
  `write_human`'s line and the rewritten docstring posture.
- **H6 settings check.** Flag semantics per F2 (statuses, sort order, exit 1 iff drift, exit 6 on
  missing SDK naming the package); fake SDKs only — grep the diff for any real import; the nightly
  step sits after `baseline`, before checkout, with no undo; dry-run lists it. VERIFY marks present
  on both vendor call shapes. The plain `judges` listing is byte-identical without the flag.
- **H7/H8 blast radius + gates.** `git diff --stat` inside the allowed paths; `make schema`,
  `make capabilities`, `make ci-adapters` zero diff; vendor-import guard untouched; ruff/mypy/
  pytest/standalone/`make check`/`make contract` tails; fixture edits kept their original assertion
  targets (especially the WP2.5 skip test — its cause must still be the missing dataset, now with a
  pinned judge).
- **Scope honesty.** No new config key, no preset change, no panel/litellm/scoring change, no
  envelope shape change beyond `checks` entries / `unpinned` contents / the new flag's own payload,
  no docs edits (the orchestrator owns CONTRACT §6, STATE 14/17, the map row).

End with the gate decision in exactly this shape:

## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
