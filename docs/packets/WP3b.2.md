# WP3b.2 packet — native judge adapters per preset (offline, fake-SDK tested)

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **388 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §3.0 (lazy-import + exit 6 rules), §6 (judge pinning), §7.4 (ImportError →
`CapabilityError` naming the install extra), `src/evalops/judges.py` (the `JudgeSpec` this WP
consumes — never modify it), `src/evalops/scoring.py` (the neutral scorer protocol: `scorer`,
`to_float`, `call_scorer`), `src/evalops/scorers/judges.py` (what a judge wrapper is today),
`src/evalops/presets.py` (`capabilities_payload`/`capabilities_text`, the generated matrix), and
`tests/unit/test_no_vendor_imports.py` (the AST guard: function-local imports are the intended
lazy pattern and fully exempt; module-level vendor imports are violations; a module-level
`try/except ImportError` handler may only contain whitelisted degradation shapes — never `raise`).
Packet template: `docs/packets/WP3b.1.md` (newest).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 388 tests
.venv/bin/python tests/run_without_pytest.py   # use the venv interpreter
make check
```

No network, no docker, **no vendor SDK installed** — and none may be installed: the doctor pins
(`tests/unit/test_presets.py`) assert vendor presets stay unreachable in `.venv`, and a leaked fake
module in `sys.modules` would corrupt those real-import pins (see E1). The four adapter SDKs are
tested against **fake modules only**; the ImportError path is tested for real (the venv genuinely
lacks them). Reuse `tests/unit/_projects.py` (`project`, `run_cli`) where a config/project fixture
is needed.

## Goal

Phase 3b is native-first (map decision: "each preset wraps the judges its backend already has").
WP3b.1 made a judge **specifiable and unpinned specs unloadable**; nothing executes one yet. This
WP delivers the four **native judge adapters** — one per evaluator backend — so the fallback WP
(3b.3) only has to fill the gap where no native judge exists, and `scorers/judges.py` becomes the
neutral wrapper layer over them. The capability matrix must say which evaluator has native judges
and what to install.

This WP adds **no** run-time wiring: `plan`/`evaluate`/`calibrate` are byte-identical to today.
Auto-selecting a scorer from a declared `JudgeSpec` is **3b.3** (it needs the LiteLLM fallback so
every spec can resolve); resolved-model recording and gate enforcement are **3b.5**.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. `src/evalops/scorers/native.py` (new core module; no module-level vendor import)

- **A1** Frozen dataclass `NativeJudge`: `key` (adapter label), `evaluator` (the evaluator **plugin
  name** it serves), `import_name` (the SDK root the factory imports), `package` (the pip-install
  name the `CapabilityError` hint shows), `description` (one line for the matrix). Exactly four
  entries in a module-level, insertion-ordered table `ADAPTERS`:

  | key | evaluator | import_name | package | vendor surface (all **VERIFY**-marked) |
  |---|---|---|---|---|
  | `mlflow-make-judge` | `mlflow` | `mlflow` | `mlflow` | `mlflow.genai.make_judge(...)` |
  | `langfuse-managed` | `langfuse` | `langfuse` | `langfuse` | managed evaluator config + score read-back (server-side execution; evalops submits the row and reads the score back) |
  | `openevals` | `langsmith` | `openevals` | `openevals` | `openevals.create_llm_as_judge(...)` |
  | `pydantic-evals` | `core` | `pydantic_evals` | `pydantic-evals` | the pydantic-evals LLM-judge metric |

  `databricks` needs no entry (its evaluator is `mlflow`); `local`/`logfire`/`mixed` need none
  (their evaluator is `core`). `adapters()` returns a sorted defensive copy; `has_native_judge(
  evaluator)` is the membership probe.
- **A2** `native_judge(spec, *, evaluator) -> Callable` builds one adapter's judge from a
  validated `JudgeSpec`:
  - evaluator not in `ADAPTERS` → `CapabilityError(evaluator, "native judge", hint=...)` → exit 6,
    hint naming the adapters that exist and that the rubric fallback is WP3b.3. Never exit 5.
  - the factory imports its SDK **inside the function body** (the guard-exempt lazy pattern) and
    translates `ImportError` into `CapabilityError(evaluator, operation, hint=f"pip install
    {package}")`, chained `from` the ImportError → exit 6. The operation string names the spec
    (`native judge '<name>'`).
  - the return value is a **neutral scorer**: `scorer(spec.name, llm=True)`-decorated callable
    `(*, inputs, outputs, expectations, trace=None)` → `float | None`, consumable by
    `call_scorer`/the core evaluator unchanged. Normalize every vendor result through
    `to_float`; a missing/unscorable result is `None`, never `0.0`.
  - the **full spec reaches the vendor call**: rubric, the pinned `spec.model`, `prompt_version`
    and `sampling` — the fake-SDK tests (E3) assert each. Where the vendor shape has no slot for a
    spec field, the adapter carries a `# VERIFY(...)` marker naming the field (resolved in phase
    3); a **silent drop without a VERIFY marker is a defect**.
- **A3** Adapter call shapes encode the current vendor docs and are honest about being unverified:
  every kwarg name, route, and placeholder syntax the vendor has not proven is `# VERIFY(...)`-
  marked (same posture as `plugins/mlflow.py`). The fakes mirror the documented shapes; the unit
  tests pin **evalops'** wrapper logic (what we pass, what we normalize), not the vendor API.
  The rubric text is passed to the vendor API (that is its purpose) but must never be logged or
  emitted by evalops — no envelope, message, or hint may contain it.

### B. `scorers/judges.py` becomes the wrapper layer

- **B1** Add `spec_judge(spec, *, evaluator)` delegating to `native.native_judge` and re-export it
  from `scorers/__init__.py`. `span_judge`/`trajectory_judge` keep their exact signatures and
  behavior (every existing test stays green and unmodified); they are wrappers over judge
  *callables*, `spec_judge` is the wrapper over *adapters*.
- **B2** No config or `run.py` wiring: a declared `JudgeSpec` still does not auto-build a scorer in
  `plan`/`evaluate`/`calibrate` (3b.3), and nothing records a resolved model per verdict (3b.5).

### C. Capability matrix (`src/evalops/presets.py`, generated docs)

- **C1** `capabilities_payload()` gains a `native_judges` key built from
  `scorers.native.adapters()` — per evaluator plugin: adapter key, package, description, and the
  presets whose `evaluator` role resolves to that plugin (mlflow → `mlflow`, `databricks`;
  langfuse → `langfuse`; langsmith → `langsmith`; core → `local`, `logfire`, `mixed`), derived
  from `PRESETS`, never restated. Deterministic and sorted like the rest of the payload.
- **C2** `capabilities_text()` renders a "Native judges" table (Evaluator / Adapter / Package /
  Presets / one-line description). `make capabilities` regenerates `docs/CAPABILITIES.md` — the
  diff is expected and belongs to this commit; after committing, zero drift. The
  `tests/fixtures/envelope/capabilities.json` golden is regenerated **once** via
  `EVALOPS_UPDATE_GOLDENS=1 pytest -q -k capabilities` (expected additive churn: the new
  `data.native_judges` key); no other golden may change.

### D. Guard extension (`tests/unit/test_no_vendor_imports.py`)

- **D1** Add `openevals` and `pydantic_evals` to `VENDOR_ROOTS` (neither is covered today:
  `pydantic_ai` does not prefix-match `pydantic_evals`), each with a planted module-level
  violation probe in the file's existing probe pattern, so the guard's claim covers the two new
  SDKs. No other guard semantics change. All four adapters must then pass with function-local
  imports only.

### E. Offline fake-SDK tests (`tests/unit/test_native_judges.py`, new)

- **E1** Fakes are `types.ModuleType` objects installed with
  `monkeypatch.setitem(sys.modules, ...)` (or an equivalent try/finally) **inside each test** —
  never at module import time, never left behind: the doctor pins import vendor modules for real
  in the same process, so a leaked `sys.modules["mlflow"]` fake would corrupt them.
- **E2** The `ImportError → CapabilityError` path is exercised **without** fakes (the real venv
  lacks all four SDKs): exit 6, the hint names the exact `package` from A1, and the error is a
  `CapabilityError` — not a traceback, not exit 5.
- **E3** Per adapter, with its fake installed: the full spec reaches the vendor call (rubric,
  pinned model — assert it is exactly `spec.model`, not a literal — prompt_version, sampling);
  results normalize through `to_float` (bool / "yes" / dict-with-score / object-with-value);
  unscorable → `None`; the scorer's name is `spec.name` with `evalops_llm` set; `call_scorer`
  consumes it unchanged. For langfuse also: the evaluator-config key is deterministic
  (judge name + `rubric_hash` + prompt_version — reuse `judges.rubric_hash`), the read-back of a
  not-yet-scored row is `None`, and no rubric text appears in any error message or repr.

## Falsifiable done-when

- **H1** Four adapters exist per A1's table; each returns a `call_scorer`-compatible neutral
  scorer named `spec.name` with `llm=True`; with a fake SDK installed, the full spec (rubric,
  `spec.model` exactly, prompt_version, sampling) reaches the vendor call; every unverified vendor
  detail carries a `# VERIFY` marker naming what is unverified.
- **H2** Without the SDK (real venv), each adapter raises `CapabilityError` → exit 6 whose hint
  names the pip package; an evaluator without an entry (`databricks` is served via `mlflow`,
  but e.g. a hypothetical `sql` evaluator) → `CapabilityError` with the A2 hint; neither path
  leaks a traceback or the rubric text.
- **H3** `scorers.judges.spec_judge` wraps `native_judge` (fake-SDK test), and
  `span_judge`/`trajectory_judge` are byte-identical to today: the existing scorer tests are
  green and unmodified, and `docs/AGENT_TRACES.md`'s described usage still works.
- **H4** `evalops capabilities --json` exits 0 with the new deterministic `data.native_judges`
  (presets derived from `PRESETS`, not restated); `capabilities.json` is the only golden change;
  `docs/CAPABILITIES.md` is regenerated (the commit diff includes it) and a second
  `make capabilities` shows zero drift; the `test_presets` drift probe stays green.
- **H5** The guard now flags module-level `openevals`/`pydantic_evals` imports (probes), all four
  adapters import lazily inside function bodies, and `make check`'s guard test is green.
- **H6** Gates: `make check` green (388 → new total), standalone runner green, `make contract`
  skips offline, `make schema` zero drift, `make ci-adapters` zero diff, no golden churn except
  `capabilities.json`, and `git status --short` lists only: `src/evalops/scorers/native.py` (new),
  `src/evalops/scorers/judges.py`, `src/evalops/scorers/__init__.py`, `src/evalops/presets.py`,
  `docs/CAPABILITIES.md` (regenerated), `tests/unit/test_native_judges.py` (new),
  `tests/unit/test_no_vendor_imports.py` (VENDOR_ROOTS + probes only),
  `tests/unit/test_presets.py` (native-judges drift assertions only),
  `tests/fixtures/envelope/capabilities.json` (regenerated). Nothing else.
- **H7** No behavior change outside the WP: `run.py`, `gate.py`, `calibrate.py`,
  `config/models.py`, core `judges.py`, `plugins/**` and `evalops.yaml` are untouched;
  `plan`/`eval`/`calibrate`/`gate` envelopes are byte-identical for every existing test; core
  stays vendor-free.
- **H8** Mutation sensitivity (reproduce on /tmp copies, report each red test): (i) hoist a vendor
  import to module level in `native.py` → the guard test red; (ii) hardcode a model string in one
  adapter instead of `spec.model` → that adapter's fake-SDK assertion red; (iii) make an
  ImportError handler return a silent `None` scorer instead of raising `CapabilityError` → H2 red;
  (iv) delete one `ADAPTERS` entry → the capability payload/matrix test red; (v) make
  `spec_judge` ignore the `evaluator` kwarg (build without the registry) → the H3/H2 wrapper test
  red.

## Allowed paths

`src/evalops/scorers/native.py` (new), `src/evalops/scorers/judges.py` (the `spec_judge` wrapper
only), `src/evalops/scorers/__init__.py` (the export only), `src/evalops/presets.py` (the
`native_judges` payload key + matrix section only), `docs/CAPABILITIES.md` (regenerated only),
`tests/unit/test_native_judges.py` (new), `tests/unit/test_no_vendor_imports.py`
(`VENDOR_ROOTS` + probes only), `tests/unit/test_presets.py` (native-judges assertions only),
`tests/fixtures/envelope/capabilities.json` (regenerated only).

## Forbidden

`src/evalops/run.py`, `gate.py`, `calibrate.py`, `pipeline.py`, core `judges.py`,
`config/**` (no schema change), `plugins/**`, `traces/**`, `scoring.py`
(`spec_judge` needs no protocol change), `pyproject.toml` (no new deps: the LiteLLM optional dep
and any extra additions are 3b.3/G3b — the CapabilityError hints name the **vendor package**, not
an evalops extra), `Makefile`, `ci/**`, `evalops.yaml`, `datasets/**`, `docs/**` except the
generated `CAPABILITIES.md`, any other envelope golden, deleting/skipping/loosening tests,
`git commit`/`push`/branch, installing any vendor SDK into `.venv`.

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
make schema && git status --short docs/evalops.schema.json
make capabilities && git diff --stat docs/CAPABILITIES.md && git status --short docs/
make ci-adapters && git status --short ci/
EVALOPS_UPDATE_GOLDENS=1 .venv/bin/python -m pytest -q -k capabilities   # only if the golden is stale
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Adapter surface
<per adapter: key → evaluator → vendor call (VERIFY count) → normalization; the registry; the wrapper>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the H8 mutation evidence>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3b.2 (native judge adapters) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`.
The packet is `docs/packets/WP3b.2.md` — read it, then `docs/CONTRACT.md` §3.0/§6/§7.4, the new
`src/evalops/scorers/native.py`, the `spec_judge` wrapper in `scorers/judges.py`, the
`native_judges` section in `presets.py`, the guard changes, and the new test file.

- **H1 honesty.** The fakes must pin *evalops'* behavior, not restate it: assert the adapter passes
  `spec.model` (mutate the spec's model in a scratch test and watch the adapter call change), the
  rubric, prompt_version and sampling — and that results go through `to_float` (feed a fake
  "yes"/dict/object and a scoreless row). A scorer returning `0.0` where the vendor produced
  nothing is a defect. Check every unverified vendor kwarg/route carries `# VERIFY`.
- **H2 real ImportError.** Run each adapter in the real venv (no fakes in `sys.modules`) and
  confirm exit 6 with the exact package in the hint, chained from ImportError, no traceback, and
  no rubric text in the message. Confirm the registry miss (unequipped evaluator) is also exit 6
  with the A2 hint. Verify no fake SDK module leaks: run the doctor pins
  (`pytest -q tests/unit/test_presets.py -k doctor`) in the same session after the adapter tests.
- **Lazy-import discipline.** Every vendor import in `scorers/native.py` is inside a function
  body. Confirm `VENDOR_ROOTS` gained `openevals` + `pydantic_evals` **with probes** (a planted
  module-level import must turn the guard red — run the drill), and that no module-level
  `try/except ImportError` was used as a loophole (the handler whitelist forbids `raise` there —
  a worker moving the translation into a module-level handler is a guard violation).
- **H3 no drift in the wrappers.** `span_judge`/`trajectory_judge` unchanged (diff is additive
  only); the existing scorer tests unmodified and green; `scorers/__init__.py` exports additive
  only; core `judges.py` untouched.
- **H4 matrix.** `evalops capabilities --json` → `data.native_judges` matches `ADAPTERS` exactly,
  presets are derived (mlflow row lists `mlflow`+`databricks`; core lists `local`+`logfire`+
  `mixed`), deterministic across runs; the regenerated `capabilities.json` differs **only** by the
  new key; `docs/CAPABILITIES.md` diff is exactly the new table + generated-note, and a second
  `make capabilities` is zero drift; the `test_presets` drift probe still fails on a mutated
  matrix byte (re-run its mutation if cheap).
- **H7 blast radius.** `git diff --stat` limited to the allowed paths; no test weakened, skipped,
  or deleted; no new dependency; `evalops.yaml`/schema untouched; envelopes byte-identical for
  plan/eval/gate (`make check` + standalone runner green; the goldens list unchanged except
  `capabilities.json`).
- **H8 mutations.** Reproduce all five on /tmp copies and name the red test each.
- **Scope honesty.** The adapters must NOT be reachable from `plan`/`evaluate`/`calibrate` —
  grep for any import of `scorers.native`/`spec_judge` outside `scorers/`, `presets.py` and tests.
  Auto-wiring is 3b.3; recording is 3b.5; both leaking into this WP is a BLOCK.

End with the gate decision in exactly this shape:

## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
