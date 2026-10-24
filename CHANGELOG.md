# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: semver, with
`FORMAT_VERSION` for persisted artifacts documented in `docs/CONTRACT.md`.

## [Unreleased]

### Added

### Added

- **WP3b.1 `JudgeSpec` (2026-10-24):** a judge is now a core, vendor-free value object — rubric,
  scale (binary/ordinal/continuous), pinned `model`, `prompt_version` and sampling — in frozen
  dataclasses, validated at load. A `judges.<name>` rule declares a spec exactly when it carries
  `rubric`/`scale`/`prompt_version`/`sampling`; threshold-only rules (the shipped `evalops.yaml`
  shape) declare none and behave byte-identically, so gate-level pin enforcement stays WP3b.5. An
  unpinned declared spec is exit 2 naming the dotted config path (absent/empty/`latest`/`*-latest`/
  `auto`/`default` model; absent/empty/`latest` prompt_version; empty rubric), as are a bad scale,
  sampling bounds and nested unknown keys. `plan`/`eval`/`calibrate` load the specs before any
  estimate, budget reservation or ledger write. Numeric validation is two-layered, both exit 2: the
  pydantic model rejects bool/±inf/NaN at the exact key (so `config validate` can never accept a
  value that serialises as `Infinity`), and `JudgeSpec.validate()` rejects the same plus float
  overflow for direct API use; a YAML integer above CPython's 4300-digit limit is now translated by
  the config loader like every other malformed yaml (exit 2 naming the file, per CONTRACT §2,
  never exit 5). `evalops judges --json` exposes the identity surface (`rubric_hash`, never the
  rubric text) plus the `unpinned` worklist, pinned by an additive golden; the config schema gains
  the four judge keys. 359 → 388 tests. Review: 2 BLOCK rounds (7 defects: `Infinity` in the
  envelope, `OverflowError`, parser digit-limit exit 5, un-normalised int bounds, vague substring
  test) + 1 orchestrator adjudication (which validation layer names the path is not a requirement;
  the test now pins the exact per-layer path) + final PASS.
- **WP2.5 calibration enforcement in the pipeline (2026-10-24):** a judge calibration below
  `judges.<name>.min_agreement` / `min_kappa` fails `evalops pipeline pr` (and `main`) at the
  `calibrate` step with exit 1 — no plan/eval/gate, no eval run row, nothing promoted — and the
  stage error now says *why*: it lists every failing check (judge, metric, required limit, measured
  value) mined from the nested `calibrate` payload, discriminated by `data.judges` so gate failure
  messages stay byte-identical. A missing calibration dataset remains a skip (pinned as a
  decision), a passing calibration proceeds unchanged, and four mutations (step removed, calibrate
  forced green, clause removed, discriminator removed) each turn the named test red. 353 → 359
  tests; review: 1 round PASS, 0 defects.
- **WP2.4 CI adapters generated from one template (2026-10-24):** GitHub Actions, GitLab CI,
  Azure Pipelines and Jenkins now render from one template in core
  (`src/evalops/ci_adapters.py`, a per-stage binding table over `pipeline.STAGES`) instead of
  hand-written prose, and **Jenkins exists for the first time**. `evalops ci-adapters` reports the
  four adapters (sorted, writes nothing) or `--write`s them; `make ci-adapters` mirrors
  `make schema`/`make capabilities`. The rendered files only bind platform events to
  `bash ci/pipeline.sh <stage>` and plumb the documented env vars — a drift test compares committed
  **bytes** to rendered bytes, a parity test requires every stage exactly once per adapter, and a
  no-logic check rejects any other evalops subcommand (narrow shell allowlist). The GitHub pr job
  checks out `refs/pull/<N>/head` from the base repository so a fork PR works with WP2.2's bot
  branch, and the generated comment states the write-token repository prerequisite; the Jenkins
  pipeline declares the `EVALOPS_ALLOW_FROZEN_CHANGE` parameter it documents, and its four `when`
  conditions are pairwise disjoint per cause (a scheduled build of `main` selects nightly only).
  329 → 353 tests. Review: 1 BLOCK (Jenkins trigger overlap, fork checkout ref, byte-level drift,
  over-broad `git` allowlist, undocumented fork-token policy, undeclared Jenkins parameter) + a
  narrow re-review PASS after the fixes.
- **WP2.2 release re-verification (2026-10-24):** `pipeline release` re-verifies before promoting:
  `guard` → `gate --group $EVIDENCE_GROUP` (a fresh re-check — a group failing today's rules stops
  the stage at exit 1 before any alias moves) → `baseline` (a recorded verification step; the
  nightly-only `baseline.json` write stays nightly) → `promote` → `commit_state`; dry-run shape
  unchanged (five steps, `<EVIDENCE_GROUP>` placeholders). `promote --alias production` binds the
  evidence group to the version it released via additive ledger tags (`evalops.released.production`,
  `evalops.released.app_version`) and refuses (exit 4, naming the group and the released version) a
  group already released — a stale group can no longer be re-released through the front door;
  `candidate`/`staging` are unchanged and re-runnable. `commit_state` routes to a force-pushed bot
  branch `refs/heads/evalops/state/pr-<EVALOPS_PR>` when `EVALOPS_PR` is set (a fork PR cannot push
  `.evalops/locks` to the protected source branch; the bot branch is derived state where the latest
  run wins, so `--force` is the correct semantics; `EVALOPS_BRANCH` is not required on that path;
  without `EVALOPS_PR` behavior is byte-identical). `pipeline nightly` with no production baseline
  fails loudly: exit 1, actionable remedy in `errors[0].message`, the control step recorded failed,
  no checkout — replacing the WP2.1 exit-0 skip (its pin was replaced, not deleted). 321 → 329
  tests; review: 1 round PASS, 0 defects; the reviewer independently reproduced all four packet
  mutations (gate-step deletion, skip revert, refusal removal, `--force` drop).
- **WP2.3 presets (2026-10-24):** `evalops init --preset <p>` renders a validated `evalops.yaml`
  for the 7 D8 presets (`local, mlflow, databricks, langfuse, langsmith, logfire, mixed`) —
  vendor URLs/keys are `${VAR:-placeholder}` interpolations, never literals, the render goes through
  the existing pydantic config models, and exactly one file is written (refusing an existing one
  without `--force`). `evalops capabilities --json` and `make capabilities` generate
  `docs/CAPABILITIES.md` from the plugin classes' own `provides` metadata and the preset table,
  with a drift test so wiring docs cannot silently diverge from code. Doctor pins assert each
  preset's real offline outcome.
- `.env.example`: every environment variable evalops reads in one templated file — core
  (`EVALOPS_ENV`, `EVALOPS_ACTOR`, `EVALOPS_CONFIG`), the ledger URL override consumed by the
  shipped yaml's `${VAR:-default}` interpolation, the `EVALOPS_REDACT` safety override, the CI
  pipeline variables, and the contract-test/golden-regen knobs. `.env` is gitignored; backend
  credentials belong in the environment, never in the committed yaml.
- **WP1.4 test debt (2026-10-24):** 53 tests (193 → 246) covering every `gate.check` rule branch
  (incl. the vacuous-gate and zero-baseline guards), `gate.reliability`/`ci_lower`/`check_rows`,
  cassette record/replay/miss/save/corruption/key-stability, `calibrate._stats` (perfect/chance/
  degenerate/threshold paths), `evalops traces` (default group, row filter, redaction), `promote`
  (all three aliases, baseline pinning, the three guard refusals) and `rollback`, `plan` budget
  reporting, and **every exit code 0–7 through a real subprocess command path** (process code and
  `envelope.code` both asserted; exit 4 was previously unasserted anywhere). 22 covered branches are
  mutation-proven: each mutation turned its named test red and the tree was restored hash-verified.
- `evalops.contract.formats` (`FORMAT_VERSION = 1`): versioned locks, cassettes, `rows.jsonl`,
  `last_eval.json`, `cost_estimate.json`, and an `evalops/manifest.json` artifact with per-artifact
  hashes. v0 artifacts still read; an unknown future version exits 7 naming the file.
- Typed configuration (`src/evalops/config/`, pydantic v2): unknown keys and wrong types are exit 2
  with a dotted path, plugin roles and `eval.row_limits` are closed sets, and
  `docs/evalops.schema.json` is generated by `make schema` with a drift test. New commands:
  `evalops config validate|schema|explain`.
- Frozen `--json` envelope `{ok, code, schema_version, command, env, data, errors}` with exit codes
  0–7 pinned by 17 golden snapshots and an exit-code matrix; `evalops --version`; a subcommand's own
  `--version` (e.g. `rollback --version 1`) is not the top-level flag.
- `docs/CONTRACT.md`: frozen exit codes (0–7), `--json` envelope v1, the five plugin port
  invariants, identity-hash rules, persisted-format policy, judge pinning policy.
- ADRs 0001–0007 (naming, license, pydantic in core, phase order, contract-test scope, judge
  pinning, exit codes + format versioning).
- Semantic-only identity hashes: cassette and dataset fingerprints and `app_version` ignore
  metadata, so stamping an artifact cannot invalidate recorded runs (pinned test constant
  `73357e6c4b54`).
- Dev loop: `tests/{unit,integration}` split, recursive pytest-free runner, ruff + mypy (strict on
  `contract`/`config`), `make check`, GitHub Actions matrix 3.10–3.12, and an AST guard proving the
  core never imports a vendor SDK (with its limits pinned by tests).

### Changed

- Malformed YAML is a usage error (exit 2) instead of an unhandled runtime failure, and argparse
  errors produce a code-2 envelope on stdout when `--json` is set.
- In `--json` mode stdout is captured at both the descriptor and stream level, so app or hook output
  cannot corrupt the document; diagnostics go to stderr.
- Exit code `7` (`CONTRACT`) added for format mismatches, raised on read only.
- Lock files carry a reserved `_meta` block; readers go through `locks.entries()`.
- `judges.<name>.max_mae` and `.threshold` accept `null`/omitted as "no gate" (both used to crash
  calibration with `float <= None` / `float(None)`).
- `pydantic>=2,<3` is a core dependency (the core stays vendor-free, not dependency-free).

### Added (WP1.1 — redaction)

- `evalops.contract.redact`: one ordered rule set (field-path globs, regex patterns, `hash` mode, a
  `module:attribute` hook) applied **after** identity hashing at exactly three outbound boundaries
  — `run._put_artifact`, `AgentTrace.to_dict`, `Cassette.save` — on deep copies, so in-memory
  identity (`app_version`, fingerprints, scorers, replay) is unchanged. Idempotent markers; non-JSON
  values are normalized through the same `default=str` tree the sinks serialize.
- Typed `redact:` config section (`mode: strict|off`, `fields`, `hash`, `patterns`, `hook`), regexes
  compiled at validation (bad pattern = exit 2), and `EVALOPS_REDACT=strict|off` to override.
- `strict` is a real write-path refusal (exit 5, nothing persisted): artifact bodies are buffered
  and re-scanned before the first ledger write, so a late refusal leaves no partial run.
- `evalops redact [--check]`: dry run over a run group's artifacts or `--file` inputs; renders the
  would-be unified diff (or capped counts in `--json`), never writes, exits 0 even on differences
  (the 0–7 exit matrix is frozen).
- `evalops/manifest.json` is derived metadata written verbatim: a rule that matches a digest cannot
  rewrite the hashes of the stored artifact bytes.
- Replay-miss diagnostics are redacted at capture time, closing a leak of tool args into
  `last_eval.json` and `data.tool_replay_misses` (CI logs).

### Changed (WP1.1)

- `ruff format --check` now covers `src/` as well as `tests/` (the phase-0 compromise); most of the
  WP1.1 diff is mechanical formatting. `make fmt` formats both trees.
- A `redacted` cassette is lossy by design: replay returns the redacted values and the reloaded
  fingerprint differs exactly when redaction changed a semantic entry field (documented in
  CONTRACT §5.1). Use `mode: off` or hash-only rules where record/replay identity must survive.
- Configuration provenance and ledger params (e.g. `behavior_config`) are not a redaction boundary:
  secrets must not live in `evalops.yaml`.

### Added (WP1.3 — statistics)

- One dispersion convention: sample standard deviation (`statistics.stdev`) under the existing
  `std` key; no gate rule depends on it. `min_ci_lower` keeps its normal-approximation meaning.
- `gate.wilson_interval` for proportions and a deterministic `gate.bootstrap_ci` (percentile of the
  mean, fixed seed). New optional rules: `gates.metrics.<m>.min_ci_lower_bootstrap` and
  `gates.reliability.min_pass_hat_k_wilson` (additive config keys, schema regenerated).
- `pass^k` documented exactly: plug-in estimator over all k repeats per case, k = minimum repeat
  count, repeats ordered by `evalops.repeat` as integers. `unordered_repeats`,
  `cases_with_fewer_repeats` and `comparable` flag input that cannot support the estimate.
- Stable row identity: `datasets.row_key` hashes `{inputs, expectations}` (duplicates get a
  deterministic `#n`), `RowResult.row_key` is additive, `rows.jsonl` carries it, and the gate aligns
  on it (`aligned_by: "row_key"`) instead of the list index; v0 rows fall back to `row_id`.
  No identity hash changes.
- Baseline pinned by group + hash: `promote --alias production` tags the baseline with
  `evalops.baseline.app_version` and `evalops.baseline.dataset_fingerprint`; the gate reports the
  pin and fails on a moved baseline or a candidate evaluated on a different dataset fingerprint.
- Drift: per metric `delta`, `pct_change`, Cohen's d, `z` and a two-sided `p` (erfc), plus paired
  drift by row key with a bootstrap CI. Additive: no existing check outcome changes.
- Change attribution: differing `app_version`/`config_hash`/`git_sha`/dataset/tool/prompt/judge/
  plugin params are named in `gate_report.md` and `data.attribution`; `behavior_config` excluded.

### Added (WP1.2 — ledger integrity)

- Ledger schema v1 (separate from `FORMAT_VERSION`): additive `evalops_meta`,
  `evalops_budget` and `evalops_budget_month` tables; `migrate()` is transactional, idempotent and
  row-preserving, and refuses a future schema version with exit 7.
- `evalops ledger migrate|verify`: `verify` is read-only and detects orphan kv/artifact/budget rows,
  manifest/hash mismatches of stored artifacts, budget total mismatches and an unknown schema
  version. Integrity problems exit 5; a ledger plugin without the capability exits 6.
- Atomic monthly budget: `budget_reserve/settle/release` on the ledger port (advisory defaults for
  non-SQL ledgers), implemented as a single re-applying upsert inside one transaction. Concurrent
  evals can no longer overspend the monthly cap; `run.evaluate` reserves `est/reps` per run, fails
  exit 3 when the cap would be exceeded, releases on failure and settles the measured cost.
- `cost.utc_month` / `cost._now_ms` make the month boundary explicit; `check_budget` projects spent
  + outstanding reservations + estimate.
- `find_runs` fetches kv rows in chunks instead of one query per run (2 statements for 20 runs).

### Fixed (WP1.4)

- `evalops traces` with no `--group` and no `.evalops/out/last_eval.json` exited 5 (runtime) for a
  missing required argument; it is now exit 2 (usage), matching CONTRACT §1 and the identical
  condition in `gate.run_gate`. Found by the new test-debt coverage, fixed minimally (+3/−1).

### Fixed (WP1.2)

- Postgres READ COMMITTED could turn a concurrent first-reserve of a new month into a
  `UniqueViolation` (exit 5); the upsert is now dialect-dispatched (`ON CONFLICT DO UPDATE` on
  SQLite/Postgres, `ON DUPLICATE KEY UPDATE` on MySQL/MariaDB, exit 6 for unverified dialects).

### Added (WP1.5 — trace model v2)

- The normalized agent trace is now a separately versioned document nested in each
  `evalops/rows.jsonl` line: `TRACE_SCHEMA_VERSION = 2`. Writers always emit 2 (`AgentTrace.to_dict`
  upgrades a document read as v1); readers accept an absent version as v1 and explicit 1–2; any
  other value is `FormatError` → exit 7 naming the artifact (`evalops/rows.jsonl`, or
  `dataset:<name>` on the calibration path). Unknown top-level and span keys are ignored.
  `FORMAT_VERSION` (the rows line) is unchanged. A v1 rows artifact still gates with the same
  checks as v2; `row_key` and `app_version` do not move.
- Unmodeled span attributes are reported, never silently dropped: `Span.unknown_attributes` holds
  sorted, deduplicated **names** only (values may be secrets) for raw attributes `normalize()` did
  not consume; `AgentTrace.unknown_attributes()` aggregates `{name: span_count}`. The additive
  surface is `evalops traces --unknown-attrs`; the default `traces` payload and its envelope golden
  are byte-identical.
- `evalops.traces.capture.BREAKER_PARITY` documents the four capture paths
  (`langchain=enforced`, `otel=post-hoc`, `openai_agents=post-hoc`, `custom=cooperative`) and
  `docs/AGENT_TRACES.md` mirrors it with the framework hard stop to set alongside. Regression tests
  cover each path, including async-only `row_limits.max_seconds` (a `TimeoutError`, never relabelled
  `budget_exceeded`); no fake abort was added to the OTel/OpenAI paths.
- `tests/fixtures/traces/rows_v1.jsonl` + `rows_v2.jsonl` pin the legacy and versioned shapes and
  the v1/v2 gate equivalence. `docs/CONTRACT.md` §5.6 scopes the identity claim honestly: the trace
  version never enters `app_version` or `row_key`, a trace-less dataset fingerprint is unchanged,
  and a dataset source record that embeds a trace document is fingerprinted verbatim (new snapshot
  by design).

### Changed (WP1.5)

- The `evalops traces` command accepts `--unknown-attrs`; without it, nothing changes. The trace
  document gains additive fields (`schema_version`, per-span `unknown_attributes`), so a v1 reader
  sees the same rows line and a v2 reader sees the new metadata.

### Added (WP2.1 — pipeline as code)

- `evalops pipeline <pr|main|release|nightly> [--dry-run]`: the CI stage table is now declarative
  Python (`src/evalops/pipeline.py`) instead of 74 lines of bash. Nested commands run in-process
  through the same CLI dispatch/capture path; a failed step propagates its own exit code (1–7),
  stops the stage, and keeps `data.steps`; the `--json` stdout is exactly one envelope and nested
  envelopes go to `.evalops/out/ci_log.jsonl`.
- `--dry-run` lists every command and action (including `commit_state` and the nightly
  checkout/restore) with placeholders for dynamic values, and writes nothing.
- `commit_state` keeps the bash rules: no-op when clean, exit 4 without `EVALOPS_BRANCH`, otherwise
  commit + push to the branch; a git failure is exit 5 with git's stderr, never a raw git code.
- Nightly now reads `data.baseline.git_sha` from the v1 envelope (the bash legacy top-level lookup
  made it always skip), skips with exit 0 when there is no baseline, and restores the original ref
  in a `finally`. The restore step records the ref/HEAD read after the call, so a skipped restore
  cannot pass vacuously.
- `ci/pipeline.sh` is a 7-line shim (`exec evalops pipeline "$@"`); the README documents the
  command. No CI adapter YAML changed (phase 2.4 generates them).

### Changed (WP2.1)

- The pipeline entry point moved from bash to `evalops`; `bash ci/pipeline.sh <stage>` keeps working
  and forwards every argument (`--dry-run`, `--json`).

## [0.3.0] - baseline

The inherited v0.3 codebase: five plugin roles, seven environments, agent trace normalization,
trajectory scorers, tool cassettes, reliability gating (pass^k), judge calibration, and a bash CI
pipeline. 17 tests.
