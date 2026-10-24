# evalops state board

Updated: 2026-10-24 · owner: orchestrator · detail: [`IMPLEMENTATION_MAP.md`](IMPLEMENTATION_MAP.md)

## Where we are

- **Phase:** **phase 2 in progress** — WP2.1 (`evalops pipeline`), WP2.2 (release
  re-verification, released-group binding, fork-PR bot branch, nightly loud failure) and WP2.3
  (presets + init + capability matrix) done; remaining: WP2.4 (CI adapters), WP2.5 (calibration
  enforcement), then G2 (tag v0.4.0). Phase 1 is closed (G1).
- **Verified base:** `86f75fa` (WP2.2); **tests:** 329 green (`make check`). **Postgres contract:**
  1 passed with `.[sql,dev]` + docker. **CI:** py3.10/3.11/3.12 green on `main` (last completed
  run: `40095127593`, WP2.2).
- **Repo:** https://github.com/minghao51/evalops (public).
- **G1 evidence (closed 2026-10-24, at 283 tests):** redaction property tests, format v0/v1 +
  future-version reads, statistics property tests, exit 0–7 through real commands, trace v1/v2
  compatibility; `make check` 283, standalone 283/283, `make schema` zero drift, goldens untouched;
  Postgres contract 1 passed (`.[sql,dev]`, docker). CI run `40078618397`.
- **Next:** WP2.4 (CI adapters: GH/GitLab/Azure/Jenkins rendered from one template, calling
  `evalops pipeline`) and WP2.5 (calibration enforcement in the pipeline) are the open phase-2
  WPs; then G2 (tag v0.4.0). Packet shape: `docs/packets/WP2.2.md` (newest) / `WP2.3.md`.
  Contract suite: `make contract` with `evalops[sql]` installed.

## Work packages

| WP | State | Commit |
|---|---|---|
| 0.0 repo + agents + map | done | `16ce2e1` |
| 0.1 contract doc + ADRs | done | `6280873` |
| 0.2 dev tooling + test layout + vendor-import guard | done | `6280873`…`09c26fb` |
| 0.4 versioned formats + semantic fingerprints | done | `6280873` |
| 0.3 typed config (pydantic) + JSON schema | done | `f0da4ce` |
| 0.5 envelope freeze + exit-code goldens | done | `7d7d8f1` |
| 0.6 STATE/CHANGELOG + first real CI run | done | `022be67` + run `40047473262` |
| 1.1 redaction (3 boundaries, strict, `redact --check`, src formatting) | done | `73c5dc7` (4 review rounds: 6 defects) |
| 1.3 statistics (sample stdev, Wilson/bootstrap, pass^k, row_key, pinned baseline, drift, attribution) | done | `eb30dd0` (1 review round: PASS, 1 cleanup) |
| 1.2 ledger integrity (schema migrate/verify, atomic budget, UTC month, N+1) | done | `2a3ac40` (2 review rounds: PG race, dialect dispatch) |
| 1.4 test debt (gate/cassette/calibrate/CLI paths, exit codes 0–7, mutation-proven) | done | `1237611` (1 review round: PASS, 0 defects; 1 bug fix: `traces` usage exit) |
| 1.5 trace model v2 (nested `schema_version`, unknown attrs, breaker parity, v1/v2 fixtures) | done | `8d0c6d9` (1 review round: PASS, 0 defects + 1 narrow re-review) |
| 2.1 `evalops pipeline <stage>` (stage table, dry-run, idempotence, bash shim) | done | `135bf0e` + exec-bit fix `b670d7b` (narrow re-review PASS) |
| 2.3 presets (`init --preset` ×7) + `evalops capabilities` + generated `docs/CAPABILITIES.md` | done | `684c629` (1 review round: PASS, 0 defects) |
| 2.2 release re-verification + released-group binding + fork-PR bot branch + nightly loud failure | done | `86f75fa` (1 review round: PASS, 0 defects) |
| **G1** phase-1 exit gate | **closed** | 2026-10-24: 283 tests, standalone 283/283, Postgres contract 1 passed, schema/goldens clean; CI `40078618397` |

## Known compromises (tracked, not forgotten)

1. **Vendor plugins remain unverified.** Every `VERIFY` marker in `plugins/` is a phase 3 contract
   test, not a claim. The `logfire` usage lookup (private SQL) is the highest risk.
2. **Redacted cassettes are lossy by design.** Replay returns redacted values and the reloaded
   fingerprint differs exactly when a rule changed a semantic entry field (a `_meta`-provided
   identity would be spoofable). Record/replay identity needs `mode: off` or hash-only rules.
   See CONTRACT §5.1.
3. **Redaction covers run payloads, not configuration.** Ledger params such as `behavior_config`
   are unredacted on purpose (config is git-versioned provenance); secrets must not live in
   `evalops.yaml` or a `behavior_config()` payload.
4. **A backend write failure mid-artifact-loop can still leave a prefix.** The strict-refusal path
   is atomic (all payloads verified before the first write); a `Ledger.put_artifact` failure after
   the loop starts is not rolled back, and no manifest exists yet for `ledger verify` to flag.
   Postgres contract tests cover the budget/ledger paths, not partial artifact publication.
5. **Budget reservations are atomic only on the SQL ledger.** Non-SQL ledgers keep the advisory
   defaults (CONTRACT §3.3.1). A run that crashes between reserve and settle leaves its reservation
   counted against the month; `verify` cannot see it (the ledger is self-consistent) and there is no
   release command yet — repair is manual SQL.
6. **MySQL budget reservation is untested.** The statement is dialect-correct (`ON DUPLICATE KEY
   UPDATE`) but no contract service exists; Postgres is the verified non-SQLite backend.
7. **A fresh/legacy SQL ledger reports `schema_version: 0`** until `evalops ledger migrate` runs;
   budget tables are created lazily and do not stamp the version. `verify` still checks what exists.
8. **pass^k is the plug-in estimator** over the *first k* repeats per case, not the combinatorial
   "at least k of n"; k is the minimum repeat count and ragged input is flagged
   (`comparable: false`). Pinned by tests and CONTRACT §9. A `min_pass_hat_k_wilson` rule without
   `reliability.success` still emits no check (pre-existing pattern).
9. **Row identity falls back to `row_id`** when any stored row lacks `row_key` (native/vendor
   evaluators that build `RowResult` themselves). Reported as `aligned_by: "row_id"`.
10. **Package name.** PyPI `evalops` / `evalops-sdk` and the GitHub org `evalops` belong to another
   product (Deixic, Inc.). Import name stays `evalops`; distribution name **decided 2026-10-24:
   `evalplane`** (ADR 0001's first preference); publication and the pre-publish availability
   re-check remain in WP6.1. See ADR 0001.
11. **The pipeline runs nested commands in-process.** `EVALOPS_ACTOR` defaults to `ci` by mutating
   the process environment; a git failure is exit 5 (bash would surface git's own code). Nightly
   `--dry-run` lists checkout/eval/gate/restore even when a missing baseline would skip, and
   dynamic values are placeholders (`<EVIDENCE_GROUP>`, `<baseline git_sha>`, …).
12. **A *drained* nightly restore that itself fails records exit 5 in `data.steps`, but that branch
   is untested** (it needs mocking `_git`). The in-plan failure path is tested, and
   `_error_envelope`'s code normalization has a unit pin.
13. **`ci_log.jsonl` is now one JSON envelope per line.** The bash pipeline teed multi-line rendered
   envelopes; line-oriented consumers still work, but the byte shape differs.

## How to work in this repo

```bash
make check            # lint + typecheck + unit/integration tests (offline)
make test             # pytest only
make test-standalone  # dependency-free runner (no pytest)
make schema           # regenerate docs/evalops.schema.json
make capabilities     # regenerate docs/CAPABILITIES.md
.venv/bin/evalops pipeline pr --dry-run   # list the CI stage steps; writes nothing
.venv/bin/evalops init --preset local --dir /tmp/new-project   # render a starter evalops.yaml
```

Rules that matter: persisted formats are additive and versioned; a green suite is not evidence of a
met requirement; no vendor import outside `plugins/` and `traces/`; never weaken a gate to pass.
