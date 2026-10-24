# WP1.4 packet — test debt: gate branches, cassettes, calibration, CLI paths, exit codes

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · 193 tests (+1 contract, deselected).
Read `docs/CONTRACT.md` §1 (exit codes) and §2 (envelope) before editing.

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 193 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

`hypothesis` is installed. Never `pip install`; report a blocker instead.

## Goal — and the one hard constraint

Close the test debt from `docs/IMPLEMENTATION_MAP.md` row 1.4: every gate branch, cassette path,
`calibrate._stats`, `evalops traces`, `promote`/`rollback`, `plan` budget failure, and **every exit
code 0–7 through a real command path**.

**Tests only.** Do not change behaviour. If a test uncovers a genuine bug: stop, report it in
`Blockers` with the failing test and the file:line, and only then (same dispatch, second message)
make the smallest fix that makes the *correct* expectation pass — never adjust the expectation to
the bug. No refactors, no renames, no threshold changes.

## Required coverage

Organize as you like (`tests/unit/test_gate_rules.py`, `test_calibrate.py`, `test_cassettes.py`,
`test_cli_commands.py`, `test_exit_codes.py` are suggested); map each item to a test name in the
report. Use temp projects/fixtures; no network, no docker for these (the contract test stays
deselected).

### A. `gate.check` (`src/evalops/gate.py`)
- **A1** metric absent from candidate → one failing check with `reason: "metric missing from candidate"`.
- **A2** `min` and `max`: pass and fail each.
- **A3** `max_drop`: within, exceeded, and `baseline is None` (no check emitted).
- **A4** `max_increase_pct`: within, exceeded, and **baseline mean 0** (guard `and b` → no check, no
   ZeroDivisionError).
- **A5** `min_ci_lower` with rows: normal bound; `n < 2` → `candidate is None` → fails.
- **A6** `min_ci_lower` vs `min_ci_lower_bootstrap` on a skewed sample where the two bounds straddle the
   threshold (one passes, the other fails).
- **A7** cost `max_cost_per_row_usd`: pass/fail, and no `cost_per_row_usd` in the candidate → no check.
- **A8** cost `max_cost_per_row_increase_pct`: pass/fail, and baseline cost absent/0 → no check.
- **A9** **empty rules** → `passed is True` and `checks == []` (pin the vacuous-gate case explicitly).

### B. `gate.reliability` / `ci_lower` / `check_rows`
- **B1** `reliability([], …)` → `{}`.
- **B2** `_row_value`: `trace.*` metrics read `trace_metrics`, plain metrics read `scores`; a missing
    value counts as success.
- **B3** `ci_lower`: `n < 2` → `None`; `n == 2` → exact pinned value with sample stdev.
- **B4** `check_rows`: `min_ci_lower` pass/fail; `min_pass_hat_k` pass/fail; `max_drop_pass_hat_k` with a
    baseline (pass/fail) and without one (no check); `min_pass_hat_k_wilson` is already covered — keep.
- **B5** reliability rule with `success: []` → no reliability checks emitted (pin the guard).
- **B6** `_needs_rows` is true for every rule kind that needs rows and false for a plain metrics-only rule.
- **B7** the `reliability (info)` entry is always `passed: True` and carries the `cand_*` fields.

### C. Cassettes (`src/evalops/tools.py`)
- **C1** record → replay round-trip: same response, `fingerprint()` stable across a reload (metadata
    additions do not move it).
- **C2** replay miss with `on_miss: error` → `ToolReplayMiss`; with `on_miss: live` → the real tool is
    called and the redacted label lands in `misses`.
- **C3** `save()` no-ops when `path is None` or nothing is dirty.
- **C4** corrupt JSON line → exit 5; a non-object line → exit 7; a future `_meta.schema_version` → exit 7
    naming the file.
- **C5** no active cassette (`use(None)` / no context) → the wrapped function runs directly and records
    nothing.
- **C6** key stability: the same args in a different dict order produce the same cassette key.

### D. `calibrate._stats` + `calibrate`
- **D1** perfect agreement → `agreement == 1.0`, `kappa == 1.0`, `mae == 0.0`.
- **D2** chance-level pairs → `kappa ≈ 0`; below-chance → negative kappa.
- **D3** the `pe >= 1` degenerate branch → `kappa == 1.0`.
- **D4** binarization: a custom `threshold`; values just below/above 0.5; `_threshold(None) == 0.5`;
    `_threshold("nope")` → exit 2.
- **D5** `calibrate` with no `judges:` and no `--scorer` → exit 2; rows with no labeled pairs → a failing
    check, never a crash; `min_agreement`/`min_kappa`/`max_mae` pass/fail; `max_mae: null` means no
    check.

### E. `evalops traces` (`run.show_traces` + CLI)
- **E1** default group comes from `.evalops/out/last_eval.json`; neither `--group` nor the file → exit 2.
- **E2** `--row` filters to one row; an unknown row → `0 row(s)` (exit 0); an unknown group → exit 2.
- **E3** the rendered `tree` is present for a traced row, and the command output contains no unredacted
    planted secret (reuse the redaction fixture pattern, no new sink).

### F. `promote` / `rollback` (`prompts.py` + CLI)
- **F1** `promote` with no runs for the evidence group → exit 2.
- **F2** evidence `evalops.promotable != "true"` → **exit 4** (`EXIT_GUARD`; this is the currently
    unasserted exit path in the suite).
- **F3** evidence without `evalops.gate == "passed"` → exit 4.
- **F4** evidence whose `prompt.*` params differ from the lock → exit 4, message names the prompts.
- **F5** `--alias candidate` and `--alias staging` move the aliases but do **not** touch
    `evalops.baseline` tags; `--alias production` moves aliases and pins the baseline (the pin check
    itself is covered in `test_gate_stats.py`; assert the end-to-end move here).
- **F6** `rollback` with an unknown prompt → exit 2; with a known prompt/version → the alias points at
    that version; `rollback --version 1` stays a subcommand flag (the envelope test covers the
    parsing; assert the resulting alias here).

### G. `plan` (`run.plan`)
- **G1** estimate above `per_run_usd` or `per_month_usd` → exit 0 with `budget.problems` populated
    (`override=True` semantics) and a "dry run" summary.
- **G2** an unpriced config → `estimate.warning` is present.
- **G3** dataset not synced → exit 2 naming `evalops sync datasets`.

### H. Every exit code through a real command path (`tests/unit/test_exit_codes.py`)
Drive the CLI (subprocess or `cli.main`) and assert **both** the process code and `envelope.code`
equal the expected code. One path per code, no direct envelope-builder calls:
- `0` a successful command (`plugins --json`);
- `1` a failed gate on a real ledger fixture;
- `2` `rollback` with an unknown prompt (and one config-key typo case);
- `3` a per-run budget refusal;
- `4` `promote` on unpromotable evidence;
- `5` a corrupt rows artifact or cassette read;
- `6` `ledger verify` on a ledger plugin without the capability (memory ledger);
- `7` a rows artifact whose `schema_version` is in the future.

## Mutation evidence (required)

For at least **10** covered branches (at least one per section A–H), temporarily break the branch in
`src/`, run the named test, confirm it fails, then restore. Suggested breakages: flip `>=` in
`ci_lower`; remove `and b` in `max_increase_pct`; invert `_needs_rows`; delete the `on_miss` branch;
make `_stats` return `kappa = 0`; make `show_traces` ignore `--row`; make `promote` skip the
`promotable` check; swap exit `4` for `2`; drop the `min_ci_lower` None check; make the cassette key
ignore args order. Report a table `branch → test → red assertion` and end with a clean
`git diff --stat` proving every mutation was reverted. Do not leave a mutation in the tree.

## Falsifiable done-when

- **H1** Every item in A–H has a named test; the report maps each to `file::test_name`.
- **H2** `tests/unit/test_exit_codes.py` exercises all of 0–7 through a real command path and asserts
   `code == envelope["code"]`; exit 4 is no longer unasserted anywhere in the suite.
- **H3** Mutation evidence table has ≥10 rows, each with the failing assertion quoted, and the final
   `git diff --stat` shows no `src/` change from the mutations.
- **H4** No existing test was deleted, skipped, or loosened (`git diff` on `tests/` shows only additions,
   except a justified replacement stated in Deviations).
- **H5** `make check` green, standalone runner green (193 → new total + the new tests), `make contract`
   still skips cleanly, `make schema` zero drift, no golden changes (no new command).
- **H6** `git status --short` lists only `tests/**` (plus `tests/fixtures/**` if you add a fixture).

## Allowed paths

`tests/unit/**` (new files + additive edits), `tests/fixtures/**` (new fixtures only).
`src/**` and `docs/CONTRACT.md` only for a genuine bug found by a test, reported first in
`Blockers`, with the failing test proving the wrong behaviour and a minimal fix that does not change
any other expectation.

## Forbidden

`docs/IMPLEMENTATION_MAP.md`, `docs/STATE.md`, `CHANGELOG.md`, `HANDOFF.md`, `.pi/**`, `.github/**`,
`pyproject.toml`, `docs/evalops.schema.json`, changing gate thresholds/semantics, deleting/skipping/
loosening tests, new dependencies, git commit/push/branch/amend.

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format src tests
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/unit/test_exit_codes.py -v
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

## Coverage map
<A–H item → tests/unit/<file>::<test>>

## Mutation evidence
| Branch (file:line) | Mutation | Test that went red | Assertion |
|---|---|---|---|

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. exit-code table and git status>

## Deviations
<none | list with reason, incl. any src fix and the bug it proved>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP1.4 (test debt) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`.
The packet is `docs/packets/WP1.4.md` — read it, then `docs/CONTRACT.md` §1–§2.

Treat the worker's coverage map and mutation table as claims. Specifically:

- **H7** **Hunt vacuous tests.** For each claimed branch, read the test and ask "what would still pass if
   this branch were deleted/inverted?" Re-run the worker's 10 mutations yourself on a `/tmp` copy
   (never modify the repo); and invent 3 of your own (e.g. make `promote` skip the gate check, make
   `_stats` use population agreement, make `plan` raise instead of reporting). Any test that stays
   green is a defect.
- **H8** **Exit codes.** Confirm all of 0–7 are driven through a real command path (no
   `envelope.from_error(...)` shortcuts) and that both the process code and `envelope.code` are
   asserted. Verify exit 4 really goes through `promote`'s guard, not a synthetic raise.
- **H9** **No behaviour change.** AST-diff every `src/**` file against HEAD: must be empty (unless a
   documented bug fix — then verify the fix is minimal, the test proves the bug, and no other test
   expectation moved). No test deletions/skips/loosened assertions; diff `tests/` and check.
- **H10** **Cassette/fixture hygiene.** Tests must not leave temp DBs/cassettes behind, must not depend on
   cwd, and must be deterministic under `pytest -q` and the standalone runner.
- **H11** **Gates unchanged.** `make check`, standalone runner, `make contract` skip, `make schema` zero
   drift, goldens untouched.
- **H12** **Report honesty.** The coverage map must name every A–H item; flag any item silently dropped or
   narrowed (e.g. "tested the helper, not the CLI path").

Verdict format exactly as the agent file defines (`## Verdict`, `## Evidence`, `## Defects`,
`## Residual risk`). BLOCK on any vacuous claim, missing A–H item, or behaviour change.
