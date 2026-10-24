# WP2.5 packet — calibration enforcement in the pipeline

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops` · `main` · **353 tests** (+1 contract, deselected).
Read `src/evalops/pipeline.py` (`_calibrate_step`, `_plan_pr`, `_plan_main`, `_step_detail`,
`_failed_entry`, `_execute_evalops`), `src/evalops/calibrate.py` (the `checks` payload and the
`ok` flag it returns), `docs/CONTRACT.md` §1 (the `ok: false` → exit 1 rule) and §2, plus the
existing pins in `tests/unit/test_pipeline.py` (dry-run table) and
`tests/integration/test_e2e_agent.py` §7 (the agent harness's `JUDGE_BROKEN` route). The map row
is phase 2 WP 2.5 in `docs/IMPLEMENTATION_MAP.md`: **calibration below
`judges.min_agreement/min_kappa` fails `pr`**.

## Environment (no system pip)

```bash
cd /home/howt/work/evalops
.venv/bin/python -m pytest -q              # 353 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No network, no docker, no new dependencies. Reuse `tests/unit/_projects.py` (`project`,
`run_cli`, `run_cli_in_process`, `add_run`, `bare_origin`, `git_out`) and the `test_pipeline.py`
helpers. `tests/fixtures/fake_app.py` already exposes a `helpful` scorer and a `judge_meta()`.

## Goal

WP2.1 put a conditional `calibrate` step in the `pr` and `main` stage tables; `calibrate` already
returns `ok: false` and exits 1 when a configured threshold is missed, and the stage already
propagates a step's exit code. What is **not** established is enforcement: nothing proves that a
below-threshold calibration actually fails a real `pipeline pr`, and the failure message
(`pipeline pr step 'calibrate' failed with exit code 1: calibrate reported a failure`) does not
name the judge, the metric, or measured-vs-required — so a CI log cannot tell an operator what to
fix.

### Step 0 — characterise before changing anything (mandatory, in the report)

Build a fixture project that has **both** a calibration dataset and a threshold it cannot meet —
`judges: {helpful: {min_agreement: 0.99}}` (or `min_kappa`) with
`datasets/judge_calibration.jsonl` rows whose `expectations.human_labels.helpful` disagree with
the `helpful` scorer's output, so `calibrate` reports `ok: false` — and run **`pipeline pr` for
real** (not `--dry-run`). Record verbatim:

- the exit code and `data.stage`;
- which step failed: its `name`, `code`, and position in `data.steps`;
- `errors[0].message` exactly as emitted;
- whether `plan`/`eval`/`gate` ran (they must not), and whether any eval run row was written;
- the same run with a **passing** threshold, to see the step succeed.

Paste both runs in `## Verification`. Then implement only the delta the characterisation proves:
if enforcement already works end-to-end, the pins below are the deliverable and **A is the only
code change**; if it does not, fix the smallest thing that makes it work and say exactly what was
broken. Do not rebuild the stage table.

### A. Actionable calibration failure (`src/evalops/pipeline.py::_step_detail`)

- **A1** Keep the existing message prefix byte-identical:
  `pipeline <stage> step '<name>' failed with exit code <code>`, then the nested reason as today.
- **A2** When the failed step's nested envelope carries `data.checks` whose entries have a
  **string `check` and a boolean `passed: false`** (this is `calibrate`'s shape:
  `{"check": "helpful: agreement >= 0.99", "passed": false, "candidate": 0.5}`), append one
  clause listing the failing checks, e.g.
  `; failing checks: helpful: agreement >= 0.99 (measured 0.5)`.
  Build it from the payload (check string + `candidate` when present) — never hard-code judge
  names, metric names or numbers.
- **A3** `gate`'s `checks` have a different shape; gate failure messages must stay
  **byte-identical** to today (assert on a gate-failure fixture or the existing pins). The
  enrichment is keyed on the calibrate shape, not on the step name.
- **A4** Nothing else moves: exactly one envelope on stdout; the enriched text appears only in
  `errors[0].message`; `data.steps[calibrate]` keeps `code: 1`, `ok: false`; the nested
  calibrate envelope still goes to stderr and `.evalops/out/ci_log.jsonl`; the process exit code
  equals `envelope.code` (1).

### B. Enforcement pins (tests — the substance of this WP)

- **B1** `pipeline pr` with a failing calibration: exit **1**, `data.stage == "pr"`, the failed
  step is `calibrate` with `code: 1`, `data.steps` keeps the steps that ran, and `plan`/`eval`/
  `gate` **never ran**; no new eval run row is written; no alias or baseline file moved.
- **B2** `pipeline main` with the same fixture: exit **1** at `calibrate` (the map names `pr`;
  `main` is pinned too so the two tables cannot drift apart silently).
- **B3** `pipeline pr` with a passing threshold: the stage proceeds past `calibrate` and the
  calibrate entry is `ok: true` (assert on `data.steps`, whatever the later steps' outcome is).
- **B4** The **absence skip stays a skip**: the existing dry-run pin (calibrate `run: false` with
  the `datasets/judge_calibration.jsonl not found` reason) passes **unmodified**, and a project
  with judges configured but no calibration dataset is not failed by `calibrate`. Pin the
  decision in a test comment: enforcement triggers on a calibration run that fails, never on a
  missing dataset (making absence fatal would break every existing `pr` pipeline and is not in
  the map row).
- **B5** Mutation sensitivity, reported as evidence: (i) delete the `calibrate` yield from
  `_plan_pr` on a `/tmp` copy → B1 goes red; (ii) make `calibrate`'s failing `ok` exit 0 on a
  `/tmp` copy → B1 goes red; (iii) remove the A2 clause → the message test goes red.

## Falsifiable done-when

- **H1** Characterisation: the before/after runs from Step 0 are pasted with exit codes, the
  failing step, the exact message, and the statement of what (if anything) had to change.
- **H2** `pr` fails at `calibrate` (exit 1) and stops: no plan/eval/gate, no run row, no alias
  change; `data.steps` present and the stage/step named.
- **H3** `main` fails the same way (exit 1 at `calibrate`).
- **H4** A passing calibration lets `pr` past the step (`ok: true` in `data.steps`).
- **H5** The absence skip is unchanged and pinned as a decision (existing dry-run test
  unmodified; B4's assertion present).
- **H6** The failure message names the judge, the metric, the required limit and the measured
  value, all derived from the payload; gate failure messages are byte-identical to before;
  envelope discipline holds (one stdout envelope, exit == code, nested envelope to
  stderr/`ci_log.jsonl`).
- **H7** Suite health: `make check` green (353 → new total), standalone runner green,
  `make contract` skips, `make schema` zero drift, `make ci-adapters` idempotent (zero drift),
  goldens untouched — if any golden genuinely changes, name it and why (none is expected:
  no pipeline-failure golden exists).
- **H8** `git status --short` lists only the allowed paths.

## Allowed paths

`src/evalops/pipeline.py`, `tests/unit/test_pipeline.py` (only if a pin must move — it should
not), a new `tests/unit/test_calibration_enforcement.py`, and `tests/integration/**` **only** if
the agent harness is the only way to produce a failing calibration (prefer the unit fixture).

## Forbidden

`src/evalops/calibrate.py`, `src/evalops/gate.py`, `src/evalops/cli.py` (no new flags),
`src/evalops/config/**`, `ci/**`, `docs/**` (`docs/CONTRACT.md` already states the `ok: false` →
exit 1 rule; if you believe it needs a sentence, report it instead of editing), `pyproject.toml`,
`tests/fixtures/**`, deleting/skipping/loosening tests, `git commit`/`push`/branch.

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
make schema && git diff --stat
make ci-adapters && git diff --stat
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Characterisation
<the failing pr run and the passing pr run: exit code, failing step, exact message, what ran>

## Enforcement
<one line each: pr failing / pr passing / main failing / absence skip / gate messages unchanged>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the B5 mutations and the exact message delta>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP2.5 (calibration enforcement in the pipeline) review, uncommitted tree at
`/home/howt/work/evalops`. The packet is `docs/packets/WP2.5.md` — read it, then
`src/evalops/pipeline.py` (`_calibrate_step`, `_plan_pr`, `_plan_main`, `_step_detail`),
`src/evalops/calibrate.py` (the `checks` shape), `docs/CONTRACT.md` §1 (the `ok: false` → exit 1
rule), and the new tests.

- **H1 characterisation honesty.** Re-run the failing and passing `pipeline pr` yourself in a
  fixture project (judges rule + `datasets/judge_calibration.jsonl` + `fake_app`'s `helpful`
  scorer). Does the worker's characterisation table match reality? If the worker claimed
  "already enforced, no behaviour change", verify that claim: exit 1 at `calibrate`, no
  plan/eval/gate, no run row.
- **H2/H3 enforcement.** `pr` and `main` with a below-threshold calibration: exit 1, failed step
  `calibrate`, steps preserved, nothing promoted, no eval row. Mutation: delete the `calibrate`
  yield from `_plan_pr` on a `/tmp` copy → the pin must go red; set `calibrate`'s failing path to
  exit 0 → red again. Vacuous assertions (e.g. asserting only on a dry-run table) are a defect.
- **H4 pass-through.** Passing calibration → `ok: true`, stage proceeds.
- **H5 absence skip.** Confirm the existing dry-run pin is *unmodified* (`git diff` the test
  file) and that a missing dataset is still a skip, with the decision pinned — not silently
  converted into a failure or into "never enforced".
- **H6 message.** Read the emitted `errors[0].message` yourself: judge, metric, required limit
  and measured value all present, derived from the payload; prefix unchanged; gate failure
  messages byte-identical to before (compare against `git stash` / `git show HEAD`); one stdout
  envelope; exit == code; nested envelope on stderr + `ci_log.jsonl`.
- **H7/H8 suite + blast radius.** `make check`, standalone runner, contract skip, schema and
  `ci-adapters` zero drift, goldens untouched; `git status --short` limited to the allowed paths;
  `calibrate.py`/`gate.py`/`cli.py`/`config/**` untouched.
- **Report honesty.** Every A–B item mapped to a named test; mutations re-run by you, not
  trusted.

Verdict format exactly as your agent file defines (`## Verdict` PASS|BLOCK, `## Evidence`,
`## Defects` numbered with file:line + concrete fix, `## Residual risk`). BLOCK on: a
below-threshold calibration that reaches plan/eval/gate, a vacuous mutation claim, an absence
skip silently turned into a failure (or into "calibration never runs"), a changed gate message,
or a test that asserts the dry-run table instead of a real run.
