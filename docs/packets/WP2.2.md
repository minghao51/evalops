# WP2.2 packet — release re-verification, fork-PR lock bot branch, nightly fails loudly

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops` · `main` · **321 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §1–§2 and §9.4, `src/evalops/pipeline.py` (the stage table whose shape this
WP must keep), `src/evalops/prompts.py::promote` (the existing refusal path you extend), and
`docs/packets/WP2.1.md` (the pipeline contract you must not break). The map row is phase 2 WP 2.2
in `docs/IMPLEMENTATION_MAP.md`.

## Environment (no system pip)

```bash
cd /home/howt/work/evalops
.venv/bin/python -m pytest -q              # 321 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No network, no docker, no new dependencies. Reuse `tests/unit/_projects.py` (`project`,
`ledger_project`, `add_run`, `run_cli`, `run_cli_in_process`, `bare_origin`, `git_out`) and the
existing `tests/unit/test_pipeline.py` helpers (`_golden_project`, `_synced`, `_steps`, `_clean`,
`_released_baseline`). Git pushes in tests go to a **local bare origin**, never a remote.

## Goal

Three gaps in the WP2.1 stage table, exactly as the map row and HANDOFF §6/WP2.2 state them:

1. `pipeline release` trusts a stale `EVIDENCE_GROUP` tag; it must re-verify **now** (guard + a
   fresh gate on the group + the current baseline) before promoting, and bind the evidence group to
   the promoted version so the same group can never be released twice.
2. A PR from a fork cannot push `.evalops/locks` to the protected source branch; `commit_state`
   must route to a **bot branch** instead of wedging the run.
3. `pipeline nightly` with no production baseline silently skips (exit 0); it must **fail loudly**
   with an actionable message.

Keep the WP2.1 stage-table/dry-run shape: declarative steps, in-process evalops commands, exact
exit-code propagation, one envelope on stdout, `data.steps` on failure, conditional steps in
`--dry-run` with `run: false` + `reason`.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. Release re-verification (`src/evalops/pipeline.py::_plan_release`)

- **A1** The release stage table becomes, in order:
  `require EVIDENCE_GROUP` (exit 2 before any step, unchanged) → `guard` →
  `gate --group $EVIDENCE_GROUP` → `baseline` →
  `promote --alias production --evidence-group $EVIDENCE_GROUP` → `commit_state`.
  Each step is the same kind of `_ev`/`_vcs` step as today; per-step exit codes propagate
  unchanged (guard 4, gate 1, gate-no-such-group 2, git 5).
- **A2** The gate step is a **fresh re-check**, not a tag read: `gate --group` re-aggregates the
  group from the ledger and applies the rules now. A group that fails the rules today stops the
  stage at the gate step (exit 1) before `promote` touches any alias.
- **A3** The `baseline` step is a recorded verification step (its envelope lands in
  `data.steps` and `.evalops/out/ci_log.jsonl` like every evalops step; it writes no file — the
  nightly-only `baseline.json` write stays nightly-only). It exits 0 on its own; the release
  decision reads nothing from it beyond the step having run.
- **A4** Dry-run shape preserved: `pipeline release --dry-run` lists all five steps with
  placeholders (`<EVIDENCE_GROUP>` in the gate/promote argv), writes nothing, and stays exit 0
  without `EVIDENCE_GROUP` (the exit-2 pre-check is real-run-only, exactly as today).

### B. Evidence-group → version binding + stale refusal (`src/evalops/prompts.py::promote`)

- **B1** On `promote --alias production` only, after the existing alias moves and baseline
  pinning, tag every run of the group **additively**:
  `evalops.released.production = "true"` and `evalops.released.app_version = <the run's
  app_version>`, using the same value the baseline pin reads (`r.params.get("app_version")`).
- **B2** Before moving anything, `promote --alias production` refuses with **exit 4 (GUARD)** when
  any run of the group already carries `evalops.released.production = "true"`: the message names
  the group, the `app_version` it was already released at, and says a fresh main-stage evidence
  group is required. This is what makes a stale group un-releasable through the front door (the
  map's "evidence-group→version binding"). `--alias candidate` / `--alias staging` write no
  released tags and do no released check (they stay re-runnable, byte-identical to today).
- **B3** These are additive ledger tags only (CONTRACT §5: ledger params/tags are additive): no
  artifact-format change, no fingerprint or `app_version` input change, no new config keys.

### C. Fork-PR locks via a bot branch (`src/evalops/_commit_state` in `pipeline.py`)

- **C1** When `EVALOPS_PR` is set at commit_state time, the state commit pushes to the bot branch
  `refs/heads/evalops/state/pr-<EVALOPS_PR>` with `git push --force origin
  HEAD:refs/heads/evalops/state/pr-<N>`. A fork PR cannot push `.evalops/locks` to the protected
  source branch; the bot branch is derived state where the latest run wins, so `--force` is the
  correct semantics (a previous run's commit must never wedge a later run of the same PR).
- **C2** The commit_state step entry records `bot_branch: "evalops/state/pr-<N>"` and the human
  line names the bot branch. `EVALOPS_BRANCH` is **not** required when `EVALOPS_PR` is set. When
  `EVALOPS_PR` is unset, behavior is byte-identical to today: exit 4 (`NO_BRANCH_MESSAGE`) when
  state changed and no branch is known; otherwise push to `EVALOPS_BRANCH` (default `main` for
  main/release).
- **C3** Dry-run renders the bot-branch push (with `<EVALOPS_PR>` as the placeholder) when the
  var is set, and today's `EVALOPS_BRANCH` push otherwise.

### D. Nightly fails loudly without a baseline (`pipeline.py::_plan_nightly`)

- **D1** When the `baseline` step reports no baseline (no `data.baseline.git_sha`), the stage
  records the control step as **failed** (`ok: false`, `code: 1`, `reason` naming the remedy) and
  exits **1**, with `errors[0].message` carrying the same actionable remedy: establish a production
  baseline (`evalops pipeline release` / `promote --alias production`) and re-run nightly. No git
  checkout runs; the restore bookkeeping stays trivially clean; `.evalops/out/baseline.json` is
  still written (the baseline step ran).
- **D2** The current exit-0 skip is obsolete **by design**: the WP1.1-era pin
  `test_nightly_without_a_baseline_skips_the_control_run` and the dry-run reason text "skipping
  the control run" are *replaced*, not deleted — the replacement pins exit 1 + the remedy message
  + no checkout, and the dry-run reason states the stage fails loudly without a baseline. Say
  exactly this in `## Deviations` and expect it in the commit body (HANDOFF non-negotiable 5).
- **D3** `evalops baseline` itself is unchanged: exit 0 with `summary: "no production baseline
  yet"`. The **stage** interprets a missing baseline as failure; the command's own contract moves.

## Falsifiable done-when

- **H1** Stage order + dry-run. A green release (fresh group from `pipeline main`) runs
  `guard` → `gate --group` → `baseline` → `promote` → `commit_state` in exactly that order
  (asserted on `data.steps` names); `pipeline release --dry-run` lists all five steps with the
  `<EVIDENCE_GROUP>` placeholder and writes nothing (extend the existing dry-run test, do not
  fork a new harness).
- **H2** Fresh-gate refusal. Build a group whose metrics fail a configured gate rule (or tighten a
  threshold on a fixture project): `pipeline release` stops at the `gate` step with **exit 1**,
  `promote` never ran (the production alias file is byte-identical), the failure envelope keeps
  `data.steps`, and `errors[0].message` names the stage and step (`pipeline release step 'gate'
  …`). Mutation-sensitive: removing the gate step from the stage table makes this test red.
- **H3** Per-step exit propagation in release: a guard violation → exit 4 before gate; an unknown
  group → exit 2 at the gate step; each stops the stage before `promote`.
- **H4** Binding + stale refusal. After a green release of group `g`: the group's runs carry
  `evalops.released.production = "true"` and `evalops.released.app_version` equal to the group's
  `app_version` param; a second `pipeline release` with the same `EVIDENCE_GROUP=g` exits **4**,
  the message names `g` and the already-released version, and neither the aliases nor the baseline
  moved. `promote --alias candidate` twice on the same group still succeeds and writes no
  released tags.
- **H5** Fork-PR path in a fixture repo (bare origin): `pipeline pr` with `EVALOPS_PR=42` and
  `EVALOPS_BRANCH` **unset**, after a state change → exit 0, the bare origin has
  `evalops/state/pr-42` (check via `git ls-remote`/`git_out`), the commit_state entry records
  `bot_branch`, and a second run with another state change exits 0 again (the branch moves —
  force semantics). Without `EVALOPS_PR`, the existing no-branch (exit 4) and push-to-branch
  tests pass **unmodified**.
- **H6** Nightly loud failure. No baseline → exit **1**; `errors[0].message` names the remedy
  (release / promote production); the control step is recorded `ok: false`, `code: 1`; no
  checkout/restore steps ran; `baseline.json` exists. With a baseline, the three existing nightly
  tests (reads `data.baseline.git_sha`; restores on failure; restore really moved HEAD back) pass
  unmodified. Mutation-sensitive: reverting the stage to exit-0 skip makes the new test red.
- **H7** Suite health: `make check` green (321 → new total), standalone runner green,
  `make contract` skips, `make schema` zero drift, **no golden changes** (if one is genuinely
  obsolete, name it and why — none is expected).
- **H8** `git status --short` lists only: `src/evalops/pipeline.py`,
  `src/evalops/prompts.py`, and new/modified test files under `tests/unit/**`. Nothing else.

## Allowed paths

`src/evalops/pipeline.py`, `src/evalops/prompts.py`, `tests/unit/**` (new files welcome, e.g.
`tests/unit/test_release_reverify.py`; modifications to `tests/unit/test_pipeline.py` where a pin
is replaced per D2/H1), `tests/integration/**` only if a promote-path pin genuinely lives there.

## Forbidden

`docs/**`, `.pi/**`, `.github/**`, `ci/**` (the adapters are WP2.4's tree), `pyproject.toml`,
`CHANGELOG.md`, `HANDOFF.md`, `src/evalops/cli.py` (no new flags: `gate --group` and `baseline`
exist), `src/evalops/run.py`, `src/evalops/gate.py`, `src/evalops/guard.py`, `src/evalops/locks.py`,
`registry.py`, `config/**`, `tests/fixtures/**` (no golden edits), deleting/skipping/loosening
tests, `git commit`/`push`/branch.

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
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Stage tables
<release: before → after; nightly no-baseline branch: before → after;
 commit_state bot-branch rule in one line>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the H2/H6 mutation evidence and the D2 replacement>

## Deviations
<none | list with reason — the D2 test replacement belongs here>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP2.2 (release re-verification, fork-PR lock bot branch, nightly loud failure) review, uncommitted
tree at `/home/howt/work/evalops`. The packet is `docs/packets/WP2.2.md` — read it, then
`docs/CONTRACT.md` §1–§2 + §9.4, `docs/packets/WP2.1.md` (the pipeline contract), and the current
`release`/`nightly` stages in `src/evalops/pipeline.py`.

- **H1 order + dry-run.** Run a real `main` → `release` in a fixture project yourself (bare
  origin; `tests/unit/_projects.py` has the helpers) and assert the step order on `data.steps`.
  Run `pipeline release --dry-run`: five steps, `<EVIDENCE_GROUP>` placeholders, no writes, exit 0.
- **H2 fresh gate.** Build a failing group yourself (tighten a gate threshold in a temp project)
  and run release: exit 1 at the gate step, promote never ran (diff the aliases file before/after),
  `data.steps` present. Then the mutation: comment the gate step out of the stage table on a /tmp
  copy — the H2 test must go red. If it stays green, the re-check is theater and the WP failed.
- **H3 propagation.** One guard-violation release (exit 4, stops before gate) and one unknown
  group (exit 2 at gate, stops before promote).
- **H4 binding + staleness.** After a green release, read the run tags (ledger query or
  `--json`): `evalops.released.production=true` and `evalops.released.app_version` equal to the
  group's `app_version`. Re-release the same group: exit 4 naming group + version, aliases and
  baseline untouched. `candidate` promote twice: still succeeds, no released tags. Check the
  tags are additive (no artifact format, fingerprint, or `app_version` change) and that
  `test_e2e_local`'s promote→rollback path still passes.
- **H5 fork-PR.** Fixture repo + bare origin, `EVALOPS_PR=42`, `EVALOPS_BRANCH` unset, dirty
  state: exit 0, origin has `evalops/state/pr-42`, entry records `bot_branch`, second run green
  (branch moved). Without `EVALOPS_PR`: the two existing commit_state tests pass unmodified.
  Dry-run with `EVALOPS_PR` set renders the bot-branch push with `<EVALOPS_PR>`.
- **H6 nightly.** No baseline: exit 1, remedy in `errors[0].message`, control step `ok:false`,
  no checkout/restore, `baseline.json` written; the three with-baseline nightly tests unmodified.
  Mutation: revert the stage to the exit-0 skip on a /tmp copy — the new test must go red. Also
  verify the old skip test was *replaced* (D2), not deleted, and the report/`## Deviations` say so.
- **Envelope discipline.** Exactly one envelope on stdout per run; nested envelopes to stderr +
  `.evalops/out/ci_log.jsonl`; exit code equals `envelope.code`; failure envelopes keep
  `data.steps` and name stage+step (the WP2.1 pattern).
- **Blast radius.** `git status --short` limited to allowed paths; `cli.py`/`run.py`/`gate.py`/
  `guard.py` untouched; no vendor imports (`test_no_vendor_imports` green); `make check`,
  standalone runner, `make contract` skip, `make schema` zero drift, goldens untouched; no
  scope-creep refactors.
- **Report honesty.** Every A–D item mapped to a named test; mutations re-run by you, not
  trusted from the report.

Verdict format exactly as your agent file defines (`## Verdict` PASS|BLOCK, `## Evidence`,
`## Defects` numbered with file:line + concrete fix, `## Residual risk`). BLOCK on: a re-release
that succeeds, a failing group that reaches promote, a nightly without baseline that exits 0, a
fork-PR run that wedges or pushes to the source branch, or any vacuous mutation claim.
