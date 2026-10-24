# HANDOFF: evalops — phase 2 in progress (WP2.1 + WP2.2 + WP2.3 + WP2.4 done; WP2.5 remains)

Repo: **https://github.com/minghao51/evalops** (public) · branch `main` · CI green on 3.10/3.11/3.12
(tests: **321 offline + 1 Postgres contract test, deselected**).
Read this file, then `docs/CONTRACT.md` (the invariants) and `docs/IMPLEMENTATION_MAP.md` (the plan).
`docs/STATE.md` is the short status board; phase 1 is closed (G1) and phase 2 has WP2.5
(calibration enforcement) open. The completed `docs/packets/WP2.2.md` / `WP2.4.md` are the packet
templates.

## 0. Current snapshot (start here)

| | |
|---|---|
| Verified base | `26d292c` (WP2.4) — CI runs `40100892525` (WP2.4 feature) green on py3.10 / 3.11 / 3.12; this docs commit sits on top |
| Tests | 353 offline (`make check`), standalone 353/353, 1 Postgres contract test (excluded) |
| Phase 1 | **closed at G1** (WP1.1–1.5) |
| Phase 2 | **WP2.1 done** (`135bf0e` + exec-bit `b670d7b`), **WP2.3 done** (`684c629`), **WP2.2 done** (`86f75fa`), **WP2.4 done** (`26d292c`); **WP2.5 remains** |
| Tree | clean; no uncommitted work |
| Next artifact | `docs/packets/WP2.5.md` (copy `docs/packets/WP2.4.md`, the newest template) |
| Gate ahead | **G2**: pipeline parity, 7 presets, calibration enforced; tag **v0.4.0** |

A second session may be working this repo: read §4's coordination rules first and fetch/rebase
before every commit. The exact opening message is at the end of §10.

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
  typed config, versioned formats, envelope and dev tooling. Phase 1 (closed, G1) added redaction
  (WP1.1), gate statistics + row identity (WP1.3), ledger integrity (WP1.2), test debt (WP1.4) and
  trace model v2 (WP1.5). Phase 2 is underway: `evalops pipeline` (WP2.1) and presets + the
  generated capability matrix (WP2.3) are in; release re-verification (2.2), CI adapters (2.4) and
  calibration enforcement (2.5) remain before G2 tags v0.4.0.

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
.venv/bin/python -m pytest -q                    # 321 tests (+1 contract deselected)
.venv/bin/python tests/run_without_pytest.py     # dependency-free runner (use the venv interpreter)
make check                                       # ruff + format + mypy + pytest (what CI runs)
make schema                                      # regenerate docs/evalops.schema.json (zero drift)
make capabilities                                # regenerate docs/CAPABILITIES.md (zero diff)
make contract                                    # Postgres suite: skips unless evalops[sql] + docker
```

The provider keys live in `~/.pi/agent/auth.json`: `deepseek` (official API — the canonical worker
model) and `opencode-go` are both set up and smoke-tested. **Do not install vendor SDKs into
`.venv`** — the WP2.3 doctor pins assert vendor presets are unreachable offline and will go red if
`mlflow`/`langfuse`/`langsmith`/`logfire` appear in the venv; contract work uses the throwaway
`/tmp/pgvenv` recipe below.

Postgres contract work (WP1.2 established this pattern):

```bash
uv venv /tmp/pgvenv --python .venv/bin/python
uv pip install --python /tmp/pgvenv/bin/python -e ".[sql,dev]"   # pytest is in the dev extra
docker compose -f tests/contract/docker-compose.yml up -d        # idempotent if already running
/tmp/pgvenv/bin/python -m pytest -q -m contract tests/contract -v
docker compose -f tests/contract/docker-compose.yml down -v      # optional; leave it to reuse
```

`make contract` in the default `.venv` skips (no psycopg); the pgvenv run above is the real check.
The container may already be running from an earlier session; `docker compose ps` first.

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

### Coordinating with the OTHER orchestrator session (read this — it happened twice)

A second agent session works this same repo with the same protocol. It delivered WP1.2, WP1.5,
the G1 close and WP2.1 while another session was working. Before you start anything:

1. **`git fetch origin` and check `git log main..origin/main` for the WP you are about to
   dispatch.** If a feature commit for your WP is already there, do NOT reimplement it: reset to
   origin, cross-check their work against your packet's done-when, and fix narrow gaps only.
2. **Before every commit: fetch, then `git pull --ff-only` / rebase.** Pushes fail with
   non-fast-forward otherwise (this happened three times on 2026-10-24).
3. **If you built a duplicate:** never delete it blind — branch it (`wp<N>-alternative-<sha>`),
   reset main to origin, and diff the two implementations for real defects in theirs. Both
   duplicate implementations so far found genuine gaps (WP1.2: none surfaced; WP2.1: the shim's
   missing exec bit → `b670d7b`). Local side branches `wp12-alternative-33ec945` and
   `wp21-alternative-fe6a1c5` exist on this machine as references.
4. **Stay in disjoint trees when racing:** if their docs commit says "WP2.4 is next", take
   WP2.5 instead (the project's own rule: two parallel workers only in disjoint trees).
5. Small increments: feature commit → push → docs commit → push, so a collision costs minutes,
   not a WP.

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
| WP1.5 trace v2 | (parallel session) | — |
| WP2.1 pipeline | (parallel session) 1 BLOCK + 2 narrow rounds; exec-bit fix `b670d7b` found by cross-check: 1 narrow round + narrow re-review | 2 |
| WP2.3 presets | 1 (PASS, 0 defects; reviewer re-rendered all 7 presets + re-ran drift mutations) | 0 |
| WP2.2 release re-verification | 1 (PASS, 0 defects; reviewer re-ran the fixture paths, the envelope check and all 4 mutations) | 0 |

Budget 1–3 rounds. Do not skip review to save time — every round found something real. If you find a
leak after a PASS (as happened in WP1.1 with replay-miss tool args), dispatch a narrow fix round and
a narrow re-review; do not commit an unreviewed change.

Commit style: one logical change per commit, imperative subject, body explains the *why*, the WP id,
and the review rounds. Never commit with a red suite. Docs commits are separate (`docs: WP1.x state…`).

## 5. Where phases 0–2 stand

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
| 2.2 release re-verification + released-group binding + fork-PR bot branch + nightly loud failure | done | `86f75fa` (1 review round: PASS, 0 defects) |

**G1 closed** (2026-10-24, at 283 tests): standalone 283/283, Postgres contract 1 passed, `make schema`
zero drift, goldens untouched; CI `40078618397`. The full checklist and evidence are in §6.

CI green on `main` (py3.10/3.11/3.12) at the milestones: `40054567708` (WP1.1),
`40058469207` (WP1.3), `40065497434` (WP1.2), `40068844584` (WP1.4), `40078618397` (WP1.5),
`40079041204` (G1), `40085640784` (WP2.1), `40089721659` (WP2.3), `40091261008` (WP2.3 handoff).

**Carried compromises** — 13 items, tracked in [`docs/STATE.md`](docs/STATE.md) §Known compromises.
Read them before promising anything. The ones that bite:
- vendor plugins remain unverified (`VERIFY` markers; logfire reads a private SQL table) — phase 3;
- a redacted cassette is lossy on reload; record/replay identity needs `mode: off` or hash-only rules;
- redaction covers run payloads, not `evalops.yaml`/ledger params;
- budget reservations are atomic only on the SQL ledger; a crash between reserve and settle leaves the
  month charged and `verify` cannot see it (no `ledger release` yet);
- MySQL budget reservation is text-verified only; a fresh/legacy SQL ledger reports `schema_version: 0`
  until `evalops ledger migrate`;
- pass^k is the plug-in estimator; row alignment falls back to `row_id` without `row_key`;
- a mid-artifact-loop backend write failure can leave a prefix (no manifest yet to flag it);
- the pipeline runs nested commands **in-process**: `EVALOPS_ACTOR` defaults to `ci` by mutating the
  process env, and a git failure is exit 5 (bash would surface git's own code);
- nightly `--dry-run` lists checkout/eval/gate/restore even when a missing baseline would skip, and
  dynamic values are placeholders; a *drained* restore that itself fails records exit 5 in
  `data.steps` but that branch is untested (needs mocking `_git`);
- `ci_log.jsonl` is now one JSON envelope per line (bash teed multi-line rendered envelopes).

## 6. Phase status — phase 1 closed (G1); phase 2 at WP2.4/2.5

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

### WP2.3 — presets, `evalops init`, capability matrix (DONE, `684c629`)

`evalops init --preset <p>` renders a validated `evalops.yaml` for the 7 D8 presets
(`local, mlflow, databricks, langfuse, langsmith, logfire, mixed`): every vendor URL/key is a
`${VAR:-placeholder}` interpolation (never a literal), the render re-parses through the pydantic
config models so an invalid yaml is unrepresentable, and exactly one file is written (refusing an
existing one without `--force`). `evalops capabilities --json` + `make capabilities` generate
`docs/CAPABILITIES.md` from the plugin classes' own `provides` metadata, with a drift test so
wiring docs cannot silently diverge from code. Doctor pins assert each preset's real offline
outcome (local healthy; vendor presets name the missing module). 16 new tests (305 → 321).
Review: 1 round PASS, 0 defects; the reviewer independently re-rendered all 7 presets, validated
schema conformance + interpolation-only credentials, and re-ran the drift mutations.

### WP2.2 — release re-verification, fork-PR locks, nightly baseline (DONE, `86f75fa`)

`pipeline release` now re-verifies before promoting: `guard` → `gate --group $EVIDENCE_GROUP`
(a **fresh** re-check — a group failing today's rules stops the stage at exit 1 before any alias
moves; deleting the gate step from the table turns its test red) → `baseline` (a recorded
verification step in `data.steps`/`ci_log.jsonl`; the nightly-only `baseline.json` write stays
nightly) → `promote` → `commit_state`; the dry-run keeps the WP2.1 shape (five steps,
`<EVIDENCE_GROUP>` placeholders). `promote --alias production` binds the group to the version it
released (additive tags `evalops.released.production` / `evalops.released.app_version`, the same
value the baseline pin reads) and refuses an already-released group with exit 4 naming the group
and version — a stale group cannot be re-released through the front door. `commit_state` routes
to a force-pushed bot branch `refs/heads/evalops/state/pr-<EVALOPS_PR>` when `EVALOPS_PR` is set
(a fork PR cannot push locks to the protected source branch; `EVALOPS_BRANCH` not required on
that path; byte-identical behavior otherwise). Nightly without a baseline **fails loudly**
(exit 1, remedy in `errors[0].message`, failed control step, no checkout) — the WP2.1 exit-0 skip
pin was replaced per the packet's D2. 8 new tests + 1 replaced (321 → 329). Review: 1 round PASS,
0 defects; the reviewer re-ran the fixture paths, the envelope discipline (raw `subprocess`
single-document check) and all four mutations on /tmp copies. Residual (non-blocking): the
`released.app_version` fallback for runs without an `app_version` param is unpinned; `main`/
`release` with `EVALOPS_PR` set also route to the bot branch (shared path) but only `pr` is
pinned. Two non-blocking reviewer nits are available as optional cleanups: `_planned_entry`
sets `entry["reason"]` in two consecutive branches that collapse to one `if step.reason:`, and the
worker's M1 mutation claim named one red test where two actually went red (stronger, not weaker).

### WP2.4 — CI adapters generated from one template (DONE, `26d292c`)

All four adapters render from one template in core (`src/evalops/ci_adapters.py`: a per-stage
binding table over `pipeline.STAGES`, four renderers, `write_adapters`), in the shape of the two
existing generators. `evalops ci-adapters` reports the four (sorted, writes nothing) or
`--write`s them; `make ci-adapters` regenerates `ci/github-actions.yml`, `ci/gitlab-ci.yml`,
`ci/azure-pipelines.yml` and the new `ci/Jenkinsfile` with zero diff. The rendered files bind
platform events to `bash ci/pipeline.sh <stage>` and plumb the documented env vars only: a drift
test compares **bytes** (a CRLF-only drift fails too), a parity test requires every stage exactly
once per adapter, and a no-logic check rejects any other evalops subcommand with a narrow shell
allowlist (`git checkout -B`/`git remote set-url`/`git fetch`, `pip install`, the shim, plus the
explicit gate/drift report sinks). Fork PRs: the pr job checks out `refs/pull/<N>/head` from the
base repository (a fork-only branch is not in origin) and `EVALOPS_PR` routes state to the WP2.2
bot branch; the generated comment documents the write-token repository setting fork PRs need.
Jenkins declares the `EVALOPS_ALLOW_FROZEN_CHANGE` parameter it documents, and its four `when`
conditions are pairwise disjoint per cause. 24 new tests (329 → 353). Review: 1 BLOCK (Jenkins
scheduled-main overlap, fork checkout ref, text-vs-bytes drift, `git push` allowlist hole,
undocumented fork token policy, undeclared Jenkins parameter) + narrow re-review PASS; the
reviewer reproduced the CRLF and `git push` mutations independently.

### WP2.5 — calibration enforcement in the pipeline (NEXT)

Can run in parallel with WP2.4 only if the other session's tree stays in `ci/**` + a renderer —
WP2.5 touches `pipeline.py`/tests (disjoint from `ci/**`). Grounding: `_calibrate_step`
(`pipeline.py`) already runs `calibrate --dataset $CALIB` conditionally on
`datasets/<CALIB>.jsonl`; enforcement means the **pr** stage fails (and the failure names the
judge and the measured vs required value) when calibration lands below
`judges.<name>.min_agreement` / `min_kappa`, per the map row — semantics + tests, not new
plumbing. Write the packet from the map's phase-2 row.

After 2.4 + 2.5: run the map's G2 checklist and tag **v0.4.0** (D10).

## 7. Remaining roadmap (phases 2 → 6)

| Phase | WPs | Deliverable | Gate / needs |
|---|---|---|---|
| **2** CLI/pipeline as code + presets | 2.1 `evalops pipeline <stage>` in Python (`--dry-run`, idempotent, bash shim) **done**; 2.2 release re-verification + fork-PR locks + nightly fails without baseline **done**; 2.3 preset engine + `evalops init --preset` (7 presets) + generated capability matrix **done**; 2.4 CI adapters generated from one template (GH/GitLab/Azure/Jenkins) **done**; 2.5 calibration enforced in the pipeline | pipeline parity, 7 presets, calibrate enforced | **G2**: tag **v0.4.0**. No external services. |
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
  pipeline.py            STAGE_TABLE (pr/main/release/nightly), step execution + exit propagation,
                         commit_state, --dry-run, ci_log.jsonl/baseline.json outputs
  presets.py             the 7 D8 presets, yaml renderer (validated), init(), capability matrix
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
ci/                      pipeline.sh (shim → `evalops pipeline`) + the four generated adapters
                         (`github-actions.yml`, `gitlab-ci.yml`, `azure-pipelines.yml`, `Jenkinsfile`)
                         rendered by `make ci-adapters` from `src/evalops/ci_adapters.py`
```

## 10. First commands in the new thread

```bash
cd evalops && make check
git fetch origin && git log --oneline main..origin/main   # is the other session ahead? which WP?
gh run list --limit 1                                      # confirm CI green on main
sed -n '/### Phase 2/,/### Phase 3b/p' docs/IMPLEMENTATION_MAP.md
```

Then:

1. **Check §4's coordination rules first.** If `origin/main` already has a WP2.5 feature commit,
   cross-check it against `docs/packets/WP2.5.md` before doing anything else; otherwise write the
   WP2.5 packet (copy `docs/packets/WP2.4.md`'s shape) and dispatch it to `evalops-worker`
   not, write the WP2.4 packet from the map's phase-2 row (copy `docs/packets/WP2.2.md`'s shape —
   it is the newest template) and dispatch it to `evalops-worker` (`deepseek/deepseek-flash`, cwd
   = the repo). Read the current `ci/*.yml` adapters and the capabilities generator
   (`make capabilities`) first — it is the code→doc pattern WP2.4 mirrors.
2. Send the working tree (untracked test files included) to `evalops-reviewer`
   (`zai/glm-5.3-flash`, thinking `high`) with the packet's done-when.
3. Loop on BLOCK; on PASS run `make check`, commit (feature + separate docs commit), fetch/rebase,
   push, and confirm CI with `gh run list`.
4. Then WP2.4 (CI adapters) and WP2.5 (calibration enforcement), toward G2 / tag `v0.4.0`.

Suggested opening message for the new thread:

> Read `HANDOFF.md`, `docs/CONTRACT.md` and `docs/STATE.md`. We are in phase 2: WP2.1 (pipeline),
> WP2.2 (release re-verification) and WP2.3 (presets + capability matrix) are done; WP2.4 and
> WP2.5 remain, then G2 tags v0.4.0. A second agent session may be working this repo — follow HANDOFF §4's coordination rules
> (fetch before dispatch, fetch/rebase before every commit, never reimplement a WP that already
> landed, stay in disjoint trees). Start WP2.4 (CI adapters: all four of GH/GitLab/Azure/Jenkins
> rendered from one template, calling `evalops pipeline`, no logic in YAML): write the packet in
> the shape of `docs/packets/WP2.2.md`, dispatch it to `evalops-worker` (deepseek/deepseek-flash),
> review with `evalops-reviewer` (zai/glm-5.3-flash, high), commit only after PASS, then push and
> check CI. If origin/main already has WP2.4, do WP2.5 (calibration enforcement) instead.
