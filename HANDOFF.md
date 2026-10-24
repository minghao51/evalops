# HANDOFF: evalops — phase 2 in progress (WP2.1 + WP2.3 done; WP2.2/2.4/2.5 remain)

Repo: **https://github.com/minghao51/evalops** (public) · branch `main` · CI green on 3.10/3.11/3.12
(tests: **321 offline + 1 Postgres contract test, deselected**).
Read this file, then `docs/CONTRACT.md` (the invariants) and `docs/IMPLEMENTATION_MAP.md` (the plan).
`docs/STATE.md` is the short status board; phase 1 is closed (G1) and phase 2 has WP2.2 (release
re-verification), WP2.4 (CI adapters) and WP2.5 (calibration enforcement) open. The completed
`docs/packets/WP2.1.md` / `WP2.3.md` are the packet templates.

---

## 1. What this is

Backend-agnostic versioning, evaluation, gating and cost control for LLM apps **and agents**.
evalops delegates execution, tracing, storage and model calls to a backend and owns only the glue:
version pinning (prompts/datasets/behavior config/judges), the 5-role plugin contract, normalized
agent traces, statistics + gates, the CLI/pipeline contract, safety (redaction, budgets, breaker) and
reproducibility (cassettes). **Wrap and normalize, never rebuild.**

- **Backends:** MLflow / Databricks, Langfuse, LangSmith, Logfire, plain git + SQL, or your plugin.
- **Interface:** one CLI for humans, CI and coding agents — stable exit codes `0–7`, frozen JSON envelope.
- **History:** v0.3.0 imported as the baseline (`0a538e2`, 17 tests). Phase 0 added the contract,
  typed config, versioned formats, envelope and dev tooling. Phase 1 added redaction (WP1.1), gate
  statistics + row identity (WP1.3) and ledger integrity (WP1.2).

## 2. Non-negotiables (read before editing anything)

1. **Contract first.** `docs/CONTRACT.md` is normative: exit codes, envelope shape, the five port
   invariants, identity-hash rules, format policy, judge pinning, redaction, statistics. Changing it
   needs an ADR + a map entry.
2. **Formats are additive and versioned.** `FORMAT_VERSION = 1` (ledger *table* schema is a separate
   version, `LEDGER_SCHEMA_VERSION = 1`). Readers accept v0; a future `schema_version` is exit 7
   naming the file, never a silent misread. Identity hashes (cassette, dataset fingerprint,
   `app_version`) cover **semantic fields only** — stamping metadata must never change them (pinned
   by the constant `73357e6c4b54`).
3. **Core is vendor-free.** No module-level import of mlflow/langfuse/langsmith/logfire/litellm/
   langchain/openai/pydantic_ai/otel/sqlalchemy outside `src/evalops/plugins/` and
   `src/evalops/traces/`. Enforced by an AST guard in `tests/unit/test_no_vendor_imports.py`.
4. **Nothing outside the envelope on stdout** in `--json` mode; diagnostics go to stderr. Exit code
   must equal `envelope.code`.
5. **Never weaken a gate, test or guard to go green.** If a test's expectation is genuinely obsolete,
   replace it and say why in the commit body and the worker report.
6. **A green suite is not evidence a requirement is met.** Reviewers falsify the done-when.

## 3. Working environment (important)

There is **no system pip** on this machine (Ubuntu, externally managed). Use the existing `.venv` or `uv`:

```bash
cd evalops
.venv/bin/python -m pytest -q                    # 246 tests (+1 contract deselected)
.venv/bin/python tests/run_without_pytest.py     # dependency-free runner
make check                                       # ruff + format + mypy + pytest (what CI runs)
make schema                                      # regenerate docs/evalops.schema.json
make contract                                    # Postgres suite: skips unless evalops[sql] + docker
```

Postgres contract work (WP1.2 established this pattern):

```bash
uv venv /tmp/pgvenv --python .venv/bin/python
uv pip install --python /tmp/pgvenv/bin/python -e ".[sql]"
docker compose -f tests/contract/docker-compose.yml up -d
/tmp/pgvenv/bin/python -m pytest -q -m contract tests/contract -v
docker compose -f tests/contract/docker-compose.yml down -v
```

Fresh clone: `uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"`.

Every environment variable evalops reads is templated in `.env.example` (core, ledger URL, redaction
override, CI pipeline vars, contract-test target); evalops reads the process environment only — it
never loads `.env` itself, and `.env` is gitignored.

## 4. How work is dispatched here (proven protocol)

Three agents, defined in `.pi/agents/` (project scope, symlinked at the session root `.pi`):

| Agent | Model | Role |
|---|---|---|
| `evalops-scout` | `deepseek/deepseek-flash` | read-only recon, compressed findings with `file:line` |
| `evalops-worker` | `deepseek/deepseek-flash` | implements exactly one WP, tests green, **no commits** |
| `evalops-reviewer` | `zai/glm-5.3-flash` @ high | adversarial review: runs commands, falsifies claims, PASS/BLOCK |

Fallback, verified 2026-10-24: `opencode-go/deepseek-v4.1-flash` (`OPENCODE_API_KEY`) runs the
worker/scout role when the official deepseek key is unavailable — same model, different gateway;
both routes were dispatched through the real agent harness and produced equivalent smoke reports
(one agent-level A/B dispatch per route).

Per WP: **packet → worker → reviewer → orchestrator commits**. The packet states goal, allowed paths,
forbidden actions, falsifiable done-when and the exact commands. Writers are serialized per module
family; at most two parallel workers in disjoint trees.

Observed review budget:

| Work | Rounds | Defects found |
|---|---|---|
| phase 0 (7 WPs) | 12 BLOCK | 35 |
| WP1.1 redaction | 4 (2 BLOCK + 1 orchestrator-found leak) | 6 |
| WP1.2 ledger | 2 (BLOCK on a live Postgres race) + 1 narrow dialect round | 3 |
| WP1.3 statistics | 1 (PASS, one cleanup) | 1 |
| WP1.4 test debt | 1 (PASS, 0 defects; 3 reviewer-invented mutations also red) | 0 (+1 bug found by tests, fixed) |

Budget 1–3 rounds. Do not skip review to save time — every round found something real. If you find a
leak after a PASS (as happened in WP1.1 with replay-miss tool args), dispatch a narrow fix round and
a narrow re-review; do not commit an unreviewed change.

Commit style: one logical change per commit, imperative subject, body explains the *why*, the WP id,
and the review rounds. Never commit with a red suite. Docs commits are separate (`docs: WP1.x state…`).

## 5. Where phase 0 + 1 stand

| WP | State | Commit |
|---|---|---|
| 0.0 repo/agents/map | done | `16ce2e1` |
| 0.1 contract + ADRs 0001–0007 | done | `6280873` |
| 0.2 test layout, ruff/mypy, `make check`, CI, vendor-import guard | done | `6280873`…`09c26fb` |
| 0.3 typed config (pydantic v2) + generated schema + `evalops config` | done | `f0da4ce` |
| 0.4 versioned formats + semantic fingerprints | done | `6280873` |
| 0.5 envelope freeze + goldens + exit matrix 0–7 | done | `7d7d8f1` |
| 0.6 state/changelog/first CI run | done | `022be67` + run `40047473262` |
| 1.1 redaction (3 boundaries, strict, `redact --check`, `src/` formatting) | done | `73c5dc7` |
| 1.3 statistics (sample stdev, Wilson/bootstrap, pass^k, row_key, pinned baseline, drift, attribution) | done | `eb30dd0` |
| WP1.2 ledger integrity (schema migrate/verify, atomic budget, UTC month, N+1) | done | `2a3ac40` |
| 1.4 test debt (gate/cassette/calibrate/CLI paths, exit codes 0–7, mutation-proven) | done | `1237611` |
| 1.5 trace model v2 (nested `schema_version`, unknown attrs, breaker parity, v1/v2 fixtures) | done | `8d0c6d9` (1 review round PASS + 1 narrow re-review) |
| 2.1 pipeline in Python (declarative stage table, `--dry-run`, in-process steps, bash shim) | done | `135bf0e` (1 BLOCK + 2 narrow PASS rounds) + exec-bit fix `b670d7b` |
| 2.3 presets (`init --preset` ×7, capability matrix from code, generated `docs/CAPABILITIES.md`) | done | `684c629` (1 review round: PASS, 0 defects) |

**G1 closed** (2026-10-24): 283 tests, standalone 283/283, Postgres contract 1 passed, `make schema`
zero drift, goldens untouched; CI `40078618397`. The full checklist and evidence are in §6.

CI green on `main` after each docs commit: `40054567708` (WP1.1), `40058469207` (WP1.3),
`40065497434` (WP1.2), `40068844584` (WP1.4), `40069033269` (WP1.4 docs), `40078618397` (WP1.5).

**Carried compromises** — 10 items, tracked in [`docs/STATE.md`](docs/STATE.md) §Known compromises.
Read them before promising anything. The ones that bite:
- vendor plugins remain unverified (`VERIFY` markers; logfire reads a private SQL table) — phase 3;
- a redacted cassette is lossy on reload; record/replay identity needs `mode: off` or hash-only rules;
- redaction covers run payloads, not `evalops.yaml`/ledger params;
- budget reservations are atomic only on the SQL ledger; a crash between reserve and settle leaves the
  month charged and `verify` cannot see it (no `ledger release` yet);
- MySQL budget reservation is text-verified only; a fresh/legacy SQL ledger reports `schema_version: 0`
  until `evalops ledger migrate`;
- pass^k is the plug-in estimator; row alignment falls back to `row_id` without `row_key`;
- a mid-artifact-loop backend write failure can leave a prefix (no manifest yet to flag it).

## 6. Phase 1 — closed (G1); phase 2 starts at WP2.1

### WP1.4 — test debt (DONE, `1237611`)

53 tests (193 → 246): every `gate.check` rule branch, `gate.reliability`/`ci_lower`/`check_rows`,
cassettes (record/replay/miss/save/corrupt/key stability), `calibrate._stats`, `evalops traces`,
`promote` (aliases + all three guard refusals), `rollback`, `plan` budget reporting, and every exit
code 0–7 through a real subprocess command path. 22 branches are mutation-proven (each mutation
turned its named test red, tree restored hash-verified). One bug found and fixed minimally: `traces`
without `--group`/`last_eval.json` now exits 2 (usage), not 5. Review round 1: PASS, 0 defects.

### WP1.5 — trace model v2 (DONE, `8d0c6d9`)

The nested trace document now carries `TRACE_SCHEMA_VERSION = 2`: writers emit 2, readers accept
absent (=v1)/1/2, anything else is exit 7 naming the artifact, and a v1 rows artifact still gates
with the same checks as v2. `Span.unknown_attributes` records the **names** (never values) of raw
attributes `normalize()` did not consume; `evalops traces --unknown-attrs` is the only surface, so
the default payload and its golden are unchanged. `BREAKER_PARITY` documents the four capture paths
(langchain enforced; otel/openai_agents post-hoc; custom cooperative) with a regression test per
path, including async-only `max_seconds`. 36 new tests (246 → 283); 1 review round PASS, 0 defects,
plus a narrow re-review that scoped the dataset-fingerprint claim (an embedded trace is fingerprinted
verbatim by design).

### G1 gate (phase 1 exit — CLOSED 2026-10-24)

Every checklist item holds; evidence:
- **Redaction proven:** the three boundaries, `strict` refusal, hypothesis idempotence tests
  (`tests/unit/test_redact.py`).
- **Formats verified:** v0 reads, v1 round-trip, future `schema_version` → exit 7, semantic
  fingerprints unchanged by metadata (`tests/unit/test_formats_v1.py`, including the nested trace
  version).
- **Statistics property-tested:** pass^k monotone, CI shrinks with n, row alignment survives a
  dataset reorder, drift names the changed param (`tests/unit/test_gate_stats.py`, hypothesis).
- **Every exit path tested:** 0–7 through real commands, exit 4 through `promote`'s guard
  (`tests/unit/test_exit_codes.py`).
- **Trace compatibility:** v1 artifacts gate, v2 round-trips, unknown attrs reported
  (`tests/unit/test_trace_format.py`, `test_trace_fixtures.py`, `test_trace_breaker.py`).
- **Gates green:** `make check` 283, standalone 283/283, `make contract` 1 passed with `.[sql]` +
  docker, `make schema` zero drift, no golden change. CI `40078618397` (py3.10/3.11/3.12).

### WP2.1 — `evalops pipeline <stage>` (DONE, `135bf0e`)

The CI stage table is now declarative Python: `evalops pipeline pr|main|release|nightly
[--dry-run]`, nested commands in-process, exact exit-code propagation (1–7), one envelope on
stdout, one JSON line per step in `.evalops/out/ci_log.jsonl`. `--dry-run` lists every
command/action and writes nothing; `commit_state` keeps the bash rules (4 without a branch, 5 on a
git failure); nightly reads `data.baseline.git_sha` (the legacy top-level bug is not reproduced),
skips with exit 0 when there is no baseline, and restores the original ref in a `finally` (the
restore step records the post-restore ref/HEAD). `ci/pipeline.sh` is a 7-line shim. 22 new tests
(283 → 305). Review: 1 BLOCK (a raw git code could produce empty stdout and a traceback) + narrow
PASS after the fix + a final PASS for the drain-code normalization.

### WP2.2 — release re-verification, fork-PR locks, nightly baseline (NEXT)

Write the packet from the map's phase-2 row. Deliverables:
- **Release re-verification:** `release` refuses an evidence group that never passed the gate, and
  re-checks guard + gate + baseline before promoting; bind the evidence group to the promoted
  version (params/tags) so a stale group cannot be promoted.
- **Fork-PR locks:** a PR from a fork cannot push locks to the source branch; handle it via a bot
  branch (or a clear refusal), with the path tested in a fixture repo.
- **Nightly baseline:** when there is no baseline, nightly **fails loudly** (non-zero, actionable
  message) instead of the current exit-0 skip.
- Keep the WP2.1 stage table/dry-run shape; add the new steps to the table and the dry-run.

## 7. Remaining roadmap (phases 2 → 6)

| Phase | WPs | Deliverable | Gate / needs |
|---|---|---|---|
| **2** CLI/pipeline as code + presets | 2.1 `evalops pipeline <stage>` in Python (`--dry-run`, idempotent, bash shim); 2.2 release re-verification + fork-PR locks + nightly fails without baseline; 2.3 preset engine + `evalops init --preset` (7 presets) + generated capability matrix; 2.4 CI adapters generated from one template (GH/GitLab/Azure/Jenkins); 2.5 calibration enforced in the pipeline | pipeline parity, 7 presets, calibrate enforced | **G2**: tag **v0.4.0**. No external services. |
| **3b** judges, native-first | 3b.1 `JudgeSpec` (pin required, unpinned = exit 2); 3b.2 native adapters (MLflow `make_judge`, Langfuse managed evaluators, openevals, pydantic-evals), `scorers/judges.py` becomes wrappers; 3b.3 LiteLLM rubric fallback + strict parsing + per-call cost; 3b.4 pairwise (position swap) + panel; 3b.5 pin enforcement, resolved model per verdict, nightly server-side settings check, calibration as gate input | every preset's default judges native + pinned + recorded | **G3b**: tag **v0.5.0**. Needs LiteLLM optional dep + fake SDK fixtures (offline). |
| **3** vendor contract verification | 3.1 docker-compose (mlflow+postgres, langfuse+clickhouse), `tests/contract/`, nightly, recorded HTTP fixtures; 3.2 resolve all 30+ `VERIFY` lines (mlflow `result_df`, langfuse trace get, langsmith `pull_prompt_commit`, logfire private SQL); 3.3 native lineage/backlinks + `evalops lineage` + gate-report lineage; 3.4 generated capability docs + SDK versions in `doctor --json` | zero unresolved VERIFY (asserted or β with a hint), lineage on ≥2 backends | **G3**. Needs **docker** + **credentials**: LangSmith/Logfire/Databricks keys — **ask the user before starting**. |
| **4** Copier template + support tiers | 4.1 `template/` Copier project (7 presets); 4.2 tiers Generated → Contract-verified → Framework-verified derived from evidence; 4.3 golden render of 7 presets + offline lifecycle + `copier update` | 7/7 lifecycle green, template published | **G4**: public repo (already public). Needs the **distribution name decision (D1)**. |
| **5** real framework trace fixtures | 5.1 real span fixtures × pinned versions (LangGraph, OpenAI Agents, ADK, Pydantic AI, CrewAI, LlamaIndex, Strands, AutoGen, Semantic Kernel, smolagents, DSPy); 5.2 verified dialect table + `traces --unknown-attrs`; 5.3 breaker parity + runaway-loop tests; 5.4 cassette parity + consent-gated record | 11 frameworks verified, drift visible in CI | **G5**. Heavy: installing 11 frameworks with pinned versions; needs user consent for recorded traces. |
| **6** release, docs, ecosystem | 6.1 publish under the D1 name + v1.0.0 + tag→build→matrix→publish workflow; 6.2 docs regeneration (README doctest, PLUGINS, AGENT_TRACES, ADR index); 6.3 plugin SDK (entry-point spec, `evalops plugin new`, conformance kit); 6.4 compatibility policy + deprecation table | installable from PyPI, 2 external plugins certified | **G6**: v1.0.0. Needs PyPI/org + LICENSE + settled name. |

Order note: **3b before 3** (D6) — judge adapters are pure code and unblock the gate; phase 3 needs
live services and credentials.

## 8. Open decisions / blockers

| Item | State | Needs |
|---|---|---|
| **Distribution name** | **Decided 2026-10-24: `evalplane`** (ADR 0001's first preference; import stays `evalops`). Publication + pre-publish availability re-check remain in WP6.1; fallbacks `agentgates`, `evalops-core`. | execute at WP6.1 |
| **LICENSE file** | ADR 0002 chose Apache-2.0; no file/metadata yet (deferred to WP6.1) | add at WP6.1 |
| **Phase 3 credentials** | docker is available; LangSmith/Logfire/Databricks need keys | **ask the user before phase 3** |
| **MySQL contract service** | budget upsert is dialect-correct but text-verified only | optional: add a mysql service to `tests/contract/` |
| **Reservation repair** | a crashed run's reservation stays charged; `verify` cannot see it | future `evalops ledger release` or expiry policy |
| **`config explain` shape** | `data.data` / `data.provenance` (kept for payload stability) | rename only with a contract bump |
| **Cost goldens** | amounts are masked (types pinned) | tighten if cost accounting regresses |

## 9. Map of the code

```text
src/evalops/
  contract/formats.py    FORMAT_VERSION, _meta, semantic hashes, FormatError(exit 7), manifest
  contract/redact.py     redaction rules + the three outbound sinks + `redact --check`
  config/                pydantic models, generated schema, load/Config/provenance, `evalops config`
  envelope.py            envelope builders, EXIT_MATRIX, one-document render
  cli.py                 argparse + dispatch + envelope + fd/stream capture + --version
  ports.py               5 role ABCs + dataclasses (RowResult incl. row_key/Usage/LoadedPrompt/RunRecord)
                         + ledger capability defaults (migrate/verify/budget_*)
  registry.py            builtin + entry-point + "module:Class" plugins, role capability check
  run.py                 plan/evaluate/baseline/show_traces (the only way evaluations run)
  gate.py                aggregate + check + pass^k/pass@k + Wilson/bootstrap + row_key alignment
                         + pinned baseline + drift + attribution
  cost.py                estimate/measure/budget (UTC month) /month spend/reservations
  calibrate.py           judge calibration vs human labels (agreement/kappa/MAE)
  guard.py               static bypass guard
  scoring.py             neutral scorer protocol + normalization + aggregation
  tools.py               cassettes (record/replay), semantic fingerprint, redacted miss labels
  locks.py               per-env locks, reserved _meta, entries()
  prompts.py datasets.py prompt/dataset sync, content-addressed snapshots, aliases, row_key/baseline pin
  traces/                model (AgentTrace), normalize (4 dialects), capture, instrument, langchain, openai_agents
  scorers/               trajectory scorers + judge adapters
  plugins/               git, sql (schema v1 + budget), core, mlflow, langfuse, langsmith, logfire
tests/
  unit/                  contract/formats, config, envelope, guard, redact, gate_stats, gate_rules,
                         ledger_sql, cost_budget, cassettes, calibrate, cli_commands, exit_codes,
                         traces, scorers, no-vendor-imports, core, _projects helper
  integration/           full lifecycle on zero-dep plugins (local + agent)
  contract/              Postgres ledger (docker compose; skipped offline)
  fixtures/              fake apps, v0 format fixtures, envelope goldens
docs/                    IMPLEMENTATION_MAP, CONTRACT, STATE, PLUGINS, AGENT_TRACES, MLFLOW_*, adr/,
                         packets/ (dispatch-ready WP packets)
ci/                      pipeline.sh + GH/GitLab/Azure adapters (phase 2 replaces with `evalops pipeline`)
```

## 10. First commands in the new thread

```bash
cd evalops && make check
gh run list --limit 1                       # confirm CI green on main
sed -n '/### Phase 1/,/### Phase 2/p' docs/IMPLEMENTATION_MAP.md
```

Then:

1. Write the WP2.2 packet from the map's phase-2 row (copy `docs/packets/WP2.1.md`'s shape) and
   dispatch it to `evalops-worker` (`deepseek/deepseek-flash`) with `cwd` = the repo. Read the
   current `release`/`nightly` stages and the G2 gate first.
2. Send the working tree (untracked test files included) to `evalops-reviewer`
   (`zai/glm-5.3-flash`, thinking `high`) with the packet's done-when.
3. Loop on BLOCK; on PASS run `make check`, commit (feature + separate docs commit), push, and
   confirm CI with `gh run list`.
4. Then WP2.3 (presets + capability matrix), WP2.4 (CI adapters), WP2.5 (calibration enforcement),
   toward G2 / tag `v0.4.0`.

Suggested opening message for the new thread:

> Read `HANDOFF.md`, `docs/CONTRACT.md` and `docs/STATE.md`. Phase 1 is closed (G1) and WP2.1 is
> done. Start WP2.2: write the packet (release re-verification + evidence-group→version binding,
> fork-PR lock handling via a bot branch, nightly fails without a baseline) in the shape of
> `docs/packets/WP2.1.md`, dispatch it to `evalops-worker` (deepseek/deepseek-flash), review with
> `evalops-reviewer` (zai/glm-5.3-flash, high), commit only after PASS, then push and check CI.
