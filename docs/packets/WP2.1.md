# WP2.1 packet — `evalops pipeline <stage>` in Python, bash shim

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **283 tests** (+1 contract, deselected).
Read `ci/pipeline.sh` (the current contract), `docs/CONTRACT.md` §1–§2, and the phase-2 row in
`docs/IMPLEMENTATION_MAP.md`.

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 283 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No network, no docker, no new dependencies. Reuse `tests/unit/_projects.py` (`project`,
`ledger_project`, `add_run`, `run_cli`, `run_cli_in_process`) and `tests/fixtures/fake_app.py`.
Git is available; tests that push use a **local bare repo**, never a remote.

## Goal

Move the CI pipeline from bash into `evalops pipeline <stage>` so the stages are declarative,
inspectable and testable, and reduce `ci/pipeline.sh` to a shim. The command must preserve the
current stage semantics and exit codes, add `--dry-run`, and be safe to re-run.

The stage table below is **normative**. Do not invent stages, reorder steps, or add guard/sync steps
that the bash script does not run.

## A. Command surface

- New module `src/evalops/pipeline.py`; a `pipeline` sub-command in `cli.py`:
  `evalops pipeline <pr|main|release|nightly> [--dry-run]`, plus the global `--json`.
- Unknown/missing stage is **exit 2** (argparse `choices` is fine; the message names the stage).
- `evalops pipeline --help` exits 0. `--dry-run` is accepted before/after the stage.
- The pipeline's `--json` stdout is exactly **one** envelope; nested command output and hook prints
  go to stderr (and the CI log), never stdout. Human mode prints a short per-step line (free-form).
- Exit code equals `envelope["code"]`. A failed step propagates **its** code (1 gate, 2 usage,
  3 budget, 4 guard, 5 runtime, 6 capability, 7 contract): do not collapse failures to 1 or 5.
  Use the CLI's `_ReportError` pattern (an `EvalOpsError` carrying `data`) so the failure envelope
  keeps `data.steps` and `errors[0]` names the stage and step.
- Run nested evalops commands **in-process** through the existing CLI dispatch/capture helpers
  (`build_parser`, `_dispatch_envelope`, `_captured_stdout`, `_forward_to_stderr`); do not spawn an
  `evalops` child process for them (tests must work without a console script on `PATH`). `git`
  actions are subprocesses.
- Set `EVALOPS_ACTOR` to `ci` when unset (the bash `:=` behaviour) before the first step.

## B. Stage table (normative; `$CALIB`, `$SMOKE`, `$GOLDEN` come from env with the bash defaults)

Env: `SMOKE_DATASET=smoke`, `GOLDEN_DATASET=golden_regression`,
`CALIBRATION_DATASET=judge_calibration`, `EVALOPS_BRANCH`, `EVALOPS_PR`,
`EVIDENCE_GROUP`. `CALIB` runs only when `datasets/<CALIB>.jsonl` exists.

| Stage | Steps, in order |
|---|---|
| `pr` | `guard` → `sync all` → **commit_state** → [`calibrate --dataset $CALIB` if present] → `plan --dataset $SMOKE --stage pr` → `eval --dataset $SMOKE --stage pr` → `gate` |
| `main` | `guard` → `sync all --check` → [`calibrate --dataset $CALIB` if present] → `plan --dataset $GOLDEN --stage main` → `eval --dataset $GOLDEN --stage main` → `gate` → **read group** from `.evalops/out/last_eval.json` → `promote --alias candidate --evidence-group <group>` → **commit_state** (`EVALOPS_BRANCH` default `main`) → record `evidence_group` |
| `release` | require `EVIDENCE_GROUP` (exit 2 if missing) → `promote --alias production --evidence-group $EVIDENCE_GROUP` → **commit_state** (`EVALOPS_BRANCH` default `main`) |
| `nightly` | `baseline` (write the envelope to `.evalops/out/baseline.json`) → if no `data.baseline.git_sha`: record a skipped control run and exit 0 → **git checkout** the baseline sha → `eval --dataset $GOLDEN --stage control` → `gate --label drift` → **git restore** the original ref in a `finally` |

Rules:
- **Stop on the first failed step** (`set -e` parity). A skipped conditional is not a failure.
- **`commit_state` parity:** `git add -A .evalops/locks .evalops/store`; if nothing is staged → no-op
  (code 0, `committed: false`); if staged and `EVALOPS_BRANCH` is unset → **exit 4** with the bash
  message (`evalops state changed but EVALOPS_BRANCH is unset; commit .evalops/locks and
  .evalops/store`); else set bot user name/email (`GIT_BOT_NAME`/`GIT_BOT_EMAIL` defaults), commit
  `chore(evalops): update versioned state [skip ci]`, and `git push origin HEAD:<branch>`. A git
  failure is **exit 5** whose message carries git's stderr; never a traceback.
- **Baseline lookup fix:** read `data.baseline.git_sha` from the v1 envelope. The bash
  `json_get .evalops/out/baseline.json "d.get('baseline')"` reads the legacy top level and is a
  latent bug; do **not** reproduce it. A present baseline must run the control eval; an absent one
  keeps the bash "skip, exit 0" behaviour (WP2.2 will make it fail).
- **Log parity:** append every nested evalops step's envelope as one JSON line to
  `.evalops/out/ci_log.jsonl` (the bash `ev ... | tee`); create `.evalops/out` only on a real run.
  `commit_state`/checkout actions are not envelopes and are not appended to the log; they appear
  only in `data.steps`.
- **No `mkdir`/writes in `--dry-run`.**

## C. `--dry-run`

- Exit 0; no evalops command runs, no file is written, no git state changes, no network.
- `data` is `{stage, dry_run: true, steps: [...]}` where each step has at least
  `name`, `kind` (`evalops` | `vcs` | `skip`), `command` (human string), `argv` (list or null),
  `run` (bool) and `reason` (when `run` is false). Conditional steps reflect the **current** file
  system (`calibrate` present/absent). Dynamic values use placeholders
  (`<group from last_eval.json>`, `<EVIDENCE_GROUP>`, `<baseline git_sha>`, `<original ref>`).
- The list must be exactly the commands the real run would execute, in order, including
  `commit_state` and the nightly checkout/restore actions.
- `release --dry-run` without `EVIDENCE_GROUP` still lists the command with `<EVIDENCE_GROUP>` and
  exits 0; the real run exits 2.

## D. Envelope `data` (real run)

- `{stage, dry_run: false, steps: [...], summary}`; each executed step carries
  `name`, `kind`, `command`, `argv`, `code`, `ok`; a skipped step carries `skipped: true` and
  `reason`. On a failure, include the failed step and all prior steps in `data.steps`.
- `main` adds `evidence_group`; `release` adds `evidence_group`; `nightly` adds `baseline_git_sha`
  when it ran. Full nested envelopes are in `ci_log.jsonl`, not duplicated in the pipeline envelope
  (keep the payload small and avoid re-emitting row payloads).
- Human mode prints one line per step (`[pr] guard ... ok`) and the evidence group for `main`.
- No golden is added for `pipeline` (new command, additive `data`; §2's frozen list is unchanged).

## E. Bash shim + docs

- Replace `ci/pipeline.sh` with a shim: `#!/usr/bin/env bash`, `set -euo pipefail`, and
  `exec evalops pipeline "$@"` (pass every arg, so `--dry-run` works and the usage message is the
  CLI's). Env vars are inherited; do not duplicate logic in bash.
- Update the README "Pipeline" section: CI calls `evalops pipeline <stage>`;
  `ci/pipeline.sh` is a thin shim; `--dry-run` lists the steps. Do **not** edit the CI YAML files
  (phase 2.4 generates adapters), `.github/**`, `docs/IMPLEMENTATION_MAP.md`, STATE/CHANGELOG/
  HANDOFF, or `pyproject.toml`.

## F. Required tests (all offline; `tests/unit/test_pipeline.py` suggested)

Use a temp git project with the `local` env (git prompts/datasets, SQL ledger or the in-memory
fixture), a `datasets` block that declares `smoke`, `golden_regression` and (for the calibrate case)
`judge_calibration`, and a local **bare** repo as `origin` when a push is expected. A calibration
fixture needs a matching `judges:` rule and pre-recorded `expectations.human_labels`; if you cannot
make it pass cleanly, test the `calibrate` step only through `--dry-run` and say so under Deviations.

1. **Dry-run parity:** for each stage, `data.steps` is the exact ordered command list (including
   placeholders); no `.evalops/out/ci_log.jsonl`, no git commit, no lock/store change.
2. **Conditional calibrate:** dry-run skips it when the dataset is absent, includes it when present.
3. **`pr` happy path:** exit 0; every step `code: 0`; `gate_report.md` exists; `ci_log.jsonl` has one
   JSON envelope per evalops step; no state commit when the tree was already synced/committed.
4. **Idempotence:** run `pr` twice on the same synced project → exit 0 both times and the second run
   creates **no** git commit (`git rev-list --count` unchanged); `sync all --check` still passes.
   Running `main` twice likewise adds no VCS commit after the first (new eval groups are expected).
5. **Failure propagation:** a failing gate (e.g. `facts/mean.min: 1.5`) → pipeline **exit 1**, the
   gate step is last, later steps are absent, `errors[0]`/`data.steps` name it. A dirty tree or
   missing lock at `guard` → **exit 4**. A tight `per_run_usd` at `eval` → **exit 3**. (Assert
   `process code == envelope["code"]` each time.)
6. **`commit_state`:** dirty state + no `EVALOPS_BRANCH` → exit 4 with the bash message; dirty state
   + branch + local bare origin → commit created and pushed, `committed: true`; clean state →
   no-op, no commit.
7. **`main` → `release` end to end:** `main` produces `evidence_group`; `release` with that group
   promotes `production` (alias points at the locked version; baseline tags pinned); without the
   group, release is exit 2. Use the local bare origin so the state commits can push.
8. **`nightly`:** no baseline → exit 0 with a skipped control step and no eval; with a baseline
   (after `release`) → checkout the baseline sha, `eval control` + `gate --label drift` run, the
   original ref is restored even when `gate` fails (simulate with a tight drift gate or a corrupt
   rows artifact), and the failure code propagates.
9. **stdout/`--json` contract:** exactly one envelope; nested envelopes are not on stdout
   (they are in `ci_log.jsonl`); a hook `print` lands on stderr, not in the envelope.
10. **Bash shim parity:** `bash ci/pipeline.sh pr --dry-run` (with the venv's `evalops` on `PATH`)
    returns the same `data.steps` as the in-process command; shim `--help`/unknown stage exit codes
    match.
11. **`EVALOPS_ACTOR` default:** with it unset, the run's `eval` actor tag is `ci` (inspect the
    ledger or the pipeline's `data`); an explicit actor is preserved.

## Allowed paths

`src/evalops/pipeline.py` (new), `src/evalops/cli.py`, `ci/pipeline.sh`, `README.md`,
`tests/unit/test_pipeline.py` (new) and additive `tests/unit/_projects.py` helpers,
`tests/fixtures/**` (new fixtures only). `src/evalops/plugins/core.py`/`run.py` only for a genuine
bug found by a test, reported in `Blockers` with the failing test and a minimal fix.

## Forbidden

`ci/*.yml`, `.github/**`, `docs/IMPLEMENTATION_MAP.md`, `docs/CONTRACT.md`, `docs/STATE.md`,
`CHANGELOG.md`, `HANDOFF.md`, `.pi/**`, `pyproject.toml`, `docs/evalops.schema.json`,
`tests/fixtures/envelope/**`, gate thresholds/semantics, deleting/skipping/loosening tests, new
dependencies, git commit/push/branch/amend.

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format src tests
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/unit/test_pipeline.py -v
.venv/bin/python tests/run_without_pytest.py
make check
make contract
make schema && git diff --stat docs/evalops.schema.json
bash ci/pipeline.sh --help >/dev/null && echo shim-ok
git status --short
```

## Falsifiable done-when

- **H1** Every stage table row is implemented in the listed order, with the exact flags; the bash
  shim delegates and contains no stage logic.
- **H2** `--dry-run` lists every command/action, reflects conditional calibrate and placeholders,
  and has **no** side effect (no file, no git state, no log).
- **H3** Nested failures propagate the exact code (1/3/4 shown by tests), stop the stage, and the
  envelope keeps `data.steps` + a named failed step; process code == envelope code.
- **H4** `commit_state` matches the bash rules (no-op, exit 4 without a branch, commit+push to a
  local bare origin), and a second `pr`/`main` adds no VCS commit.
- **H5** `main → release` moves `production` and pins the baseline; missing `EVIDENCE_GROUP` is
  exit 2; `nightly` uses `data.baseline.git_sha` (the bash legacy lookup is not reproduced), skips
  without a baseline, runs control+drift with one, and restores the ref in a `finally`.
- **H6** `--json` stdout is exactly one envelope; `ci_log.jsonl` has one line per evalops step;
  hook prints are stderr.
- **H7** 283 → new total green in both runners; `make contract` skips; `make schema` zero drift; no
  golden/schema change; no new dependency.
- **H8** `git status` lists only the allowed paths; no test deleted/skipped/loosened.

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Coverage map
<A–F item → tests/unit/test_pipeline.py::<test>>

## Mutation evidence
| Branch (file:line) | Mutation | Test that went red | Assertion |
|---|---|---|---|

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the stage table, exit codes, dry-run, idempotence, shim, git status>

## Deviations
<none | list with reason, incl. the bash baseline-lookup fix and any src fix>

## Blockers
<none | what is needed>
```

## Mutation evidence (required)

At least **8** mutations, at least one per section A–F, each with the named test that goes red and the
quoted assertion, then reverted. Suggested: drop `commit_state` from `pr`; swap the exit-1 step code
for 5; make `--dry-run` still execute `guard`; invert the calibrate condition; ignore
`EVIDENCE_GROUP`; read the legacy `d["baseline"]` path; skip the nightly ref restore; make
`commit_state` commit when nothing is staged; make the pipeline print nested envelopes to stdout.
End with `git diff --stat` proving only intended changes remain.

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP2.1 (pipeline in Python) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`.
Read `docs/packets/WP2.1.md`, the current `ci/pipeline.sh` semantics and `docs/CONTRACT.md` §1–§2.
Treat the worker's coverage map, mutation table and claims as claims.

- **P1** **Stage parity.** Compare the Python stage table line-by-line with the bash script: order,
  flags, env defaults, conditional calibrate, the main evidence-group hand-off, release, the
  nightly baseline/checkout/restore. Flag any added/removed/reordered step and any silent behaviour
  change other than the documented baseline-lookup fix.
- **P2** **Dry-run truth.** Does `--dry-run` list exactly what the real run executes (including
  `commit_state`, checkout/restore and placeholders) and truly write nothing? Mutate a step to run
  under `--dry-run` and confirm a test goes red. Check `release --dry-run` without
  `EVIDENCE_GROUP`.
- **P3** **Exit codes.** Verify 1/3/4 (and generically 2/5/6/7) propagate from a nested step, that
  the stage stops, and that `data.steps` survives the failure envelope. Confirm `commit_state`
  without a branch is 4, git failure is 5, unknown stage is 2, and process code == envelope code.
  No `sys.exit` inside library code.
- **P4** **Idempotence and VCS.** Re-run `pr` (and `main`) twice: no new commit on the second run,
  no lock/store drift, no duplicate log lines that misrepresent steps. Check the local-bare-origin
  push test is real (not mocked) and that a clean state is a no-op.
- **P5** **Nightly.** The baseline SHA must come from `data.baseline`; absent baseline skips with
  exit 0; present baseline runs control+drift; the original ref is restored in a `finally` even on
  failure. Confirm the bash legacy `d.get('baseline')` bug is **not** reproduced.
- **P6** **stdout/log contract.** `--json` is one envelope; nested envelopes only in
  `ci_log.jsonl`; hook prints to stderr. Check fd-level capture is reused, not reimplemented badly.
- **P7** **Scope/hygiene.** `git diff` only within the allowed paths; no CI YAML, schema, golden,
  gate/threshold or test deletion; `make check`/standalone/contract/schema green; temp projects
  cleaned up; no cwd dependence; both runners.
- **P8** **Honesty.** Every A–F item has a real test; flag narrowed claims. Re-run the worker's ≥8
  mutations plus 3 of your own on a `/tmp` copy (never modify the repo); any test that stays green
  is a defect.

Verdict format exactly as the agent file defines (`## Verdict`, `## Evidence`, `## Defects`,
`## Residual risk`). BLOCK on any stage-parity gap, vacuous dry-run/idempotence claim, wrong exit
code, unreviewed scope, or golden/schema movement.
