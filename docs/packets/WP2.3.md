# WP2.3 packet — preset engine, `evalops init --preset`, capability matrix from code

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops` · `main` · **305 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §1–§2, `evalops.yaml` (the shape a preset must render), and
`docs/PLUGINS.md` (the hand-written matrix this WP makes generatable).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops
.venv/bin/python -m pytest -q              # 305 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No network, no docker, no vendor SDK installs. Reuse `tests/unit/_projects.py` for temp projects.

## Goal

Make the 7 presets (D8: `local, mlflow, databricks, langfuse, langsmith, logfire, mixed`) real:
a preset engine in core, `evalops init --preset <p>` that renders a **valid** `evalops.yaml`, and a
capability matrix **generated from code** so plugin wiring docs can't drift. `evalops doctor`
already reports per-role connectivity — this WP pins what doctor must report for each preset
offline.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. Preset engine (`src/evalops/presets.py`, new core module)

- **A1** `PRESETS: dict[str, PresetSpec]` with **exactly** the D8 names. `PresetSpec` is a frozen
  dataclass: `name`, `description`, `environments` (the evalops.yaml `environments:` mapping as
  Python data — every rendered key must already exist in the pydantic config models; no new config
  keys), `extras` (pip extras to install, e.g. `["mlflow"]`), `roles` (role → plugin map for the
  matrix).
- **A2** Wiring per preset (the env key each preset renders is that preset's own name, plus the
  standard `local` env always present so users can always fall back offline):
  - `local`: git/git/sql/core (exactly the shipped `evalops.yaml` local env — sqlite ledger);
  - `mlflow`: mlflow for all five roles (extra `mlflow`);
  - `databricks`: mlflow for all five roles with a databricks host placeholder (extra `databricks`);
  - `langfuse`: langfuse prompts/datasets/tracer/evaluator + sql ledger (extra `langfuse`);
  - `langsmith`: langsmith prompts/datasets/tracer/evaluator + sql ledger (extra `langsmith`);
  - `logfire`: logfire tracer + git prompts/datasets + sql ledger + core evaluator (extra `logfire`);
  - `mixed`: git prompts/datasets + sql ledger + **logfire** tracer + core evaluator — the showcase
    that roles compose; the rendered yaml comments list every legal plugin per role.
  Vendor URLs/keys are rendered as `${VAR:-placeholder}` interpolations, never literals — same rule
  as the shipped yaml. Logfire needs its private-SQL caveat noted in the rendered comments (STATE
  compromise 1).
- **A3** The engine validates before writing: rendering goes through the existing pydantic config
  models (`evalops init` must never be able to write a yaml that `config validate` rejects). Core
  stays vendor-free; no new dependencies.

### B. `evalops init --preset <p> [--force] [--dir <path>]`

- **B1** Writes exactly one file: `<dir>/evalops.yaml` (default dir: cwd). Appends nothing to
  `.gitignore`, creates nothing else. Human output prints the extras to install and the next three
  commands (`config validate`, `doctor`, `sync all`).
- **B2** Existing `evalops.yaml` → exit **2** unless `--force` (which overwrites). Unknown preset →
  exit **2** with all 7 valid names in the message. `--json` envelope rules as every command;
  `data` = `{preset, files_written, extras, next_steps}`.

### C. Capability matrix from code

- **C1** `evalops capabilities --json`: `data.presets` = one entry per preset (role wiring, extras,
  description) and `data.plugins` = one entry per built-in plugin (roles provided, from the plugin
  classes' own `provides` metadata — read it from `registry`/plugin classes, do not restate it by
  hand). Exit 0, deterministic (sorted), golden-stable.
- **C2** `make capabilities` regenerates `docs/CAPABILITIES.md` from the same code path (a small
  renderer next to the schema generator's pattern). A drift test fails when the matrix changes
  without regeneration — mirror how `make schema` is drift-tested today.
- **C3** `docs/PLUGINS.md` is untouched (it stays the hand-written narrative; CAPABILITIES.md is the
  generated table). No tiers/verified status in this WP — that is WP4.2's evidence-derived tiers.

### D. Doctor expectations per preset

- **D1** For each of the 7 presets, a test renders it into a temp project and runs
  `evalops doctor --json`, asserting the **actual** offline outcome per preset: `local` → healthy
  (exit 0); each vendor preset → the documented-unavailable outcome doctor already produces
  (missing SDK/endpoint), with the error naming the plugin and the pip extra to install. These pins
  are regression guards on doctor's reporting, not claims that vendor presets work offline.

## Falsifiable done-when

- **H1** Exactly 7 presets, D8 names. For each: `init --preset <p>` → render → `evalops config
  validate` passes (loop test over all presets; no hand-written yaml in tests).
- **H2** `init` refuses an existing file (exit 2), `--force` overwrites byte-for-byte deterministically
  (two renders of the same preset are identical), unknown preset → exit 2 naming all 7, and only
  `evalops.yaml` is ever written.
- **H3** The 7 doctor pins of §D1 pass and are mutation-sensitive: flip `local`'s ledger from sql to
  a nonexistent plugin in the preset table (on a /tmp copy) and the local doctor pin goes red.
- **H4** `evalops capabilities --json` is deterministic and golden-pinned (`tests/fixtures/envelope/capabilities.json`);
  `make capabilities` regenerates `docs/CAPABILITIES.md` with **zero diff**; the drift test goes red
  if a preset's wiring changes without regeneration (mutation on a /tmp copy).
- **H5** `make check` green (305 → new total), standalone runner green, `make contract` skips,
  `make schema` zero drift (no config-model change), only the new goldens added (`init` and/or
  `capabilities` — regenerate, never hand-edit).
- **H6** `git status --short` lists only: `src/evalops/presets.py`, `src/evalops/cli.py` (two new
  subcommands), `Makefile` (one additive target), `docs/CAPABILITIES.md` (generated), new test
  files, new goldens. Nothing else.

## Allowed paths

`src/evalops/presets.py` (new), `src/evalops/cli.py` (the two new subcommands only), `Makefile`
(one additive `capabilities` target beside `schema`), `docs/CAPABILITIES.md` (generated output
only), `tests/unit/**` + `tests/integration/**` (new files), `tests/fixtures/envelope/*.json`
(new goldens via `EVALOPS_UPDATE_GOLDENS=1`).

## Forbidden

`docs/**` hand-edits (CAPABILITIES.md only via the generator), `CONTRACT.md`, `IMPLEMENTATION_MAP.md`,
`STATE.md`, `CHANGELOG.md`, `HANDOFF.md`, `.pi/**`, `.github/**`, `pyproject.toml` (no new deps),
`src/evalops/config/**` (the rendered keys already exist; if a preset genuinely cannot render
without a new config key, stop and report a blocker instead of adding one), `registry.py`
(read-only), deleting/skipping/loosening tests, `git commit`/`push`/branch.

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
make capabilities && git diff --stat docs/CAPABILITIES.md
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Preset table
<per preset: role wiring, extras, rendered env keys>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the H3/H4 mutation evidence>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP2.3 (presets + init + capability matrix) review, uncommitted tree at `/home/howt/work/evalops`.
The packet is `docs/packets/WP2.1.md`'s sibling `docs/packets/WP2.3.md` — read it, then
`docs/CONTRACT.md` §1–§2 and the shipped `evalops.yaml`.

- **H1 hunt (render validity).** For every preset, run `init --preset` into a temp dir and
  `config validate` the result yourself. Check the rendered yaml: only pre-existing config keys,
  `${VAR:-placeholder}` interpolation for every vendor URL/key (a literal credential in a preset is
  a defect), `local` env present in all 7, and the D8 wiring table of §A2 followed exactly.
- **H2 refusal paths.** Overwrite refusal (exit 2), `--force` overwrite, unknown preset exit 2
  listing all 7 names, exactly one file written. Determinism: render twice, byte-identical.
- **H3/H4 mutations.** On /tmp copies: change `local`'s wiring → the doctor pin and the drift test
  must go red; change a preset's extras → the capabilities golden must go red. A matrix that cannot
  drift-fail is the defect this WP exists to prevent.
- **Generated-doc honesty.** `docs/CAPABILITIES.md` must be regenerable (`make capabilities` → zero
  diff) and must be *derived* (cross-check 2–3 rows against the plugin classes' `provides`) — not a
  hand-written table wearing a generator's clothes. `docs/PLUGINS.md` untouched.
- **No behaviour change outside the WP.** `git diff` limited to allowed paths; `config/**` and
  `registry.py` untouched; core vendor-free (`test_no_vendor_imports` green); `make check`,
  standalone runner, `make contract` skip, `make schema` zero drift; only new goldens.
- **Report honesty.** Every A–D item mapped to a named test; the doctor pins must assert doctor's
  real output (run one yourself), not a mock.

Verdict format exactly as your agent file defines (`## Verdict` PASS|BLOCK, `## Evidence`,
`## Defects` numbered with file:line + concrete fix, `## Residual risk`). BLOCK on any invalid
rendered yaml, literal credential, hand-written matrix, or missing mutation sensitivity.
