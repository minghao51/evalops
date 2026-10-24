# WP3b.1 packet — `JudgeSpec` in core (pin required, unpinned rejected at load)

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **359 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §1 (exit codes), §1.1 (config keys are a model change + runtime in one
commit) and §6 (judge pinning — the normative rules this WP makes representable), `src/evalops/
scorers/judges.py` (what a judge is today: a plain callable + `judge_meta`), `src/evalops/scoring.py`
(the scorer protocol), `JudgeRule` in `src/evalops/config/models.py`, and how `plan`/`evaluate`/
`calibrate` build scorers (`run.py::_build_scorers`, `run.py::plan`, `calibrate.py::calibrate`).
Packet template: `docs/packets/WP2.5.md` (newest).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 359 tests
.venv/bin/python tests/run_without_pytest.py   # use the venv interpreter
make check
```

No network, no docker, no vendor SDKs (and none may be imported — the AST guard
`tests/unit/test_no_vendor_imports.py` must stay green). Reuse `tests/unit/_projects.py` (`project`,
`run_cli`) and `tests/unit/test_envelope.py`'s shared temp-project fixture for the golden.

## Goal

Phase 3b makes judges native-first, pinned and recorded. Before adapters (3b.2) or enforcement
(3b.5) exist there must be a **core, vendor-free value object that can express a judge**: its
rubric, scale, pinned model, prompt version and sampling — and that **cannot represent an unpinned
judge**. Today a "judge" is an anonymous callable plus a free-form `judge_meta()` dict; nothing
validates that the model is a concrete pin, so every downstream WP (adapters, resolved-model
recording, nightly drift checks, calibration-as-gate-input) would have to re-invent validation.

This WP adds `JudgeSpec`, its config surface, and load-time rejection. It adds **no** judge
execution, no SDK, no adapter.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. `src/evalops/judges.py` (new core module; no vendor import)

- **A1** Frozen dataclasses:
  - `Scale(max: float, min: float = 0.0, kind: str = "binary")` with `kind in {"binary",
    "ordinal", "continuous"}`; `binary` requires `(min, max) == (0.0, 1.0)`.
  - `Sampling(temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None)`.
  - `JudgeSpec(name, rubric, model, prompt_version, scale=Scale(), sampling=Sampling())` —
    constructible only through `JudgeSpec.from_rule(...)` / `specs(...)`, which validate; a direct
    constructor call with an unpinned model is allowed only because dataclasses cannot intercept it,
    so validation lives in classmethods **and** a public `validate()` (exit 2) that the classmethods
    call. Do not use pydantic here (core `ports.py`-style dataclasses; config stays the only pydantic
    surface).
- **A2** `RULE_KEYS`/`SPEC_KEYS`: a `judges.<name>` rule declares a **spec** iff any of `rubric`,
  `scale`, `prompt_version`, `sampling` is present. A rule with only calibration thresholds
  (`min_agreement`/`min_kappa`/`max_mae`/`label`/`threshold`) and/or `model` declares **no** spec:
  `specs()` yields `None` for it and behaviour is byte-identical to today. (This is what keeps the
  shipped `evalops.yaml` and every existing test working; global pin enforcement is 3b.5.)
- **A3** `JudgeSpec.validate()` raises `EvalOpsError(..., EXIT_USAGE)` naming the **dotted config
  path** (`judges.<name>.<key>`) for every failure:
  - `rubric` non-empty after `strip()`;
  - `model` is a **concrete pin**: non-empty, and not one of the alias/default forms — `default`,
    `latest`, `*-latest` (e.g. `gpt-latest`), `auto`, `project-default`, `default/*`, `*/*/default`;
    comparison is case-insensitive on the trimmed value. A pin may be any other non-empty string
    (a vendor model id is not validated by evalops — it is recorded, not resolved);
  - `prompt_version` non-empty and not `latest`;
  - `scale.kind` in the closed set; `min < max`; `binary` ⇒ `(0.0, 1.0)`; when the rule sets
    `threshold`, it must lie within `[min, max]`;
  - `sampling.temperature` in `[0, 2]`, `max_tokens` a positive int, `seed` an int or `None`.
- **A4** `specs(cfg) -> dict[str, JudgeSpec | None]` reads `cfg.section("judges")`, builds and
  validates every declared spec (fail fast on the first invalid one in sorted name order), and
  returns one entry per rule in config. `load(cfg) -> dict[str, JudgeSpec]` returns only the
  declared specs. `payload(cfg) -> dict` is the deterministic, JSON-safe surface for the CLI:
  per spec `{name, model, prompt_version, scale, sampling, rubric_chars, rubric_hash}` where
  `rubric_hash = sha256(rubric.encode())[:12]` (the rubric **text is never emitted** — reviewers and
  CI logs get a stable identity, not a wall of prose), plus `unpinned`: the sorted names of rules
  that carry calibration thresholds but **no** spec (the 3b.5 worklist surfaced early).

### B. Config surface (`src/evalops/config/models.py` + regenerated schema)

- **B1** `JudgeRule` gains four optional fields: `rubric: str | None`, `scale: ScaleRule | None`,
  `prompt_version: str | None`, `sampling: SamplingRule | None`. `ScaleRule`/`SamplingRule` are
  `_Strict` submodels with exactly the §A1 keys (an unknown nested key is exit 2 with its dotted
  path, per CONTRACT §1.1). `model` already exists and stays `str | None`.
- **B2** No new top-level config section; `judges` stays the section. `make schema` regenerates
  `docs/evalops.schema.json` — the diff is expected and belongs to this commit; after committing,
  `make schema` must show zero drift.
- **B3** Validation order: pydantic (types/unknown keys, exit 2) → `JudgeSpec.validate()` (pins and
  ranges, exit 2). A spec's semantic errors are never swallowed by a schema error.

### C. Load-time rejection (`run.py`, `calibrate.py`)

- **C1** `plan()`, `evaluate()` and `calibrate()` call `judges.load(cfg)` **before** any cost
  estimate, budget reservation or ledger write, so an invalid spec is exit 2 with nothing spent and
  no run row. The call is a pure read; when no spec is declared it returns `{}` and every existing
  behaviour (including `judge_meta()`-only judges) is unchanged.
- **C2** Do not thread the specs into scorers yet (that is 3b.2's adapter work) and do not record
  them per verdict (3b.5). This WP only makes an unpinned declared spec unrepresentable and
  load-rejected.

### D. `evalops judges [--json]` (cli.py: one additive subcommand)

- **D1** Reads the config; `data` = `judges.payload(cfg)` (`{judges: [...], unpinned: [...]}`).
  Deterministic (sorted by name), exit 0. Human mode prints one line per judge
  (`name  model  prompt_version  scale  rubric_hash`) and a warning line for each `unpinned` name.
- **D2** It is a new command after the envelope freeze, so it is pinned by an **additive** golden
  `tests/fixtures/envelope/judges.json` produced through the existing `tests/unit/test_envelope.py`
  fixture (`EVALOPS_UPDATE_GOLDENS=1`); the §2 golden list for pre-existing commands is unchanged.
  The fixture project must declare at least one full spec and one threshold-only rule so both list
  branches appear in the golden.

## Falsifiable done-when

- **H1** `JudgeSpec` round-trips a fully specified judge from `judges.<name>` and exposes exactly
  `{name, model, prompt_version, scale, sampling, rubric_chars, rubric_hash}`; `rubric_hash` is the
  first 12 hex of sha256(rubric) and the rubric text never appears in `payload()` or the CLI output.
- **H2** Every invalid pin is exit **2** naming the dotted path: `model` absent, `""`, `latest`,
  `gpt-latest`, `auto`, `default`; `prompt_version` absent/empty/`latest`; empty/whitespace rubric.
- **H3** Scale/sampling validation: unknown `kind`; `min >= max`; `binary` with `(0, 5)`;
  `threshold` outside the scale; `temperature` 2.5 or -0.1; `max_tokens` 0; a nested unknown key
  (`judges.x.scale.banana`) → exit 2 with the dotted path.
- **H4** Load-time rejection is real and free: `plan`, `eval` and `calibrate` on a project with an
  invalid declared spec exit **2** before any ledger row exists
  (`evalops ledger verify`/`find_runs` show no run) and before any cost estimate is emitted.
- **H5** Backward compatibility: a rule with only `min_agreement`/`min_kappa` (the shipped
  `evalops.yaml` shape) declares no spec, `load()` returns `{}`, `evalops judges --json` lists it
  under `unpinned`, and `plan`/`eval`/`calibrate` behave exactly as before (existing tests green,
  unmodified).
- **H6** `evalops judges --json` is deterministic, one envelope, golden-pinned; `make schema` shows
  a diff **only** because of B1 (inspect it: the four new properties under `judges.*`), and a
  subsequent `make schema` is zero drift.
- **H7** `make check` green (359 → new total), standalone runner green, `make contract` skips,
  `make capabilities`/`make ci-adapters` zero diff, core stays vendor-free, no golden churn except
  the new `judges.json`, and `git status --short` lists only: `src/evalops/judges.py` (new),
  `src/evalops/config/models.py`, `docs/evalops.schema.json` (regenerated), `src/evalops/run.py`,
  `src/evalops/calibrate.py`, `src/evalops/cli.py`, `tests/unit/test_judge_spec.py` (new),
  `tests/unit/test_envelope.py`, `tests/fixtures/envelope/judges.json` (new). Nothing else.
- **H8** Mutation sensitivity (reproduce on /tmp copies, report the red test): (i) accept `latest`
  in the pin check → H2 red; (ii) make `prompt_version` optional → H2 red; (iii) drop the
  `judges.load(cfg)` call from `evaluate` → H4 red; (iv) emit `rubric` text instead of the hash →
  H1 red.

## Allowed paths

`src/evalops/judges.py` (new), `src/evalops/config/models.py` (the four `JudgeRule` fields +
`ScaleRule`/`SamplingRule`), `docs/evalops.schema.json` (regenerated only), `src/evalops/run.py`
(the `judges.load(cfg)` call only), `src/evalops/calibrate.py` (the same call),
`src/evalops/cli.py` (the `judges` subcommand only), `tests/unit/test_judge_spec.py` (new),
`tests/unit/test_envelope.py` (the new golden case), `tests/fixtures/envelope/judges.json` (new
golden via `EVALOPS_UPDATE_GOLDENS=1`).

## Forbidden

`src/evalops/gate.py` (pin enforcement at the gate is 3b.5), `src/evalops/scorers/**` (adapters are
3b.2 — do not change `span_judge`/`trajectory_judge`), `src/evalops/pipeline.py`, `ci/**`,
`Makefile`, `evalops.yaml`, `datasets/**`, `docs/**` (CONTRACT/STATE/CHANGELOG/HANDOFF are the
orchestrator's), `pyproject.toml` (no new deps), `.env.example`, other envelope goldens,
deleting/skipping/loosening tests, `git commit`/`push`/branch.

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
make capabilities && git status --short docs/
make ci-adapters && git status --short ci/
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Spec surface
<field → rule → exit code; the declared-vs-threshold-only discriminator; the unpinned worklist>

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

WP3b.1 (`JudgeSpec` in core) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`.
The packet is `docs/packets/WP3b.1.md` — read it, then `docs/CONTRACT.md` §1/§1.1/§6, the new
`src/evalops/judges.py`, `JudgeRule` in `config/models.py`, and the load-time calls in `run.py`/
`calibrate.py`.

- **H1/H2/H3 hunt.** Drive the validation matrix yourself: absent/empty/`latest`/`gpt-latest`/
  `auto`/`default` model, absent/empty/`latest` prompt_version, whitespace rubric, bad scale
  (`min >= max`, binary ≠ 0..1, threshold outside), sampling bounds, and a nested unknown key.
  Each must be exit 2 naming `judges.<name>.<key>` — not a traceback, not exit 5.
- **H4 honesty.** Prove load rejection is *before* spend: invalid spec → `plan`/`eval`/`calibrate`
  exit 2, no ledger run row, no cost estimate. Also prove a **valid** spec changes nothing about
  how scorers are built today (no adapter wiring leaked in).
- **H5 compatibility.** The shipped `evalops.yaml` describes a threshold-only judge; confirm it
  yields `load() == {}`, appears under `unpinned`, and that every existing test is unmodified and
  green. A spec that is silently inferred for threshold-only rules (or silently ignored for full
  ones) is a defect.
- **The discriminator.** Judge `SPEC_KEYS`: can a user declare `model` alone (no rubric) and
  believe they have a spec while nothing validates? Is that the documented 3b.5 line, or an
  accidental silent skip? Check the packet's A2 and CONTRACT §6 against the implementation.
- **Identity/rubric handling.** `rubric_hash` must be over the exact rubric text, stable across
  runs, and the rubric text must not leak into `payload()`/golden/human output. Check the golden
  for accidental prose.
- **H6 golden.** Verify `evalops judges --json` is deterministic and that
  `EVALOPS_UPDATE_GOLDENS=1` regenerated only `judges.json`; the schema diff must contain exactly the
  four new properties (and their nested submodels) and nothing else.
- **H8 mutations.** On /tmp copies: accept `latest`; make `prompt_version` optional; drop the
  `judges.load()` call from `evaluate`; emit rubric text instead of the hash. Each must turn its
  named test red.
- **No behaviour change outside the WP.** `git diff` limited to allowed paths; `scorers/**`,
  `gate.py`, `pipeline.py` untouched; config schema is the only generated-doc change; no golden
  churn beyond the new one; core vendor-free; `make check` + standalone runner green.
- **Report honesty.** Every A–D item mapped to a named test; run the mutation drills yourself.

End with the gate decision in exactly this shape:

## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
