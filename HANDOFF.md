# HANDOFF: evalops — phase 2 closed at G2 (`v0.4.0`); phase 3b WPs all done (WP3b.5 at `e484552`) — next: close G3b, tag `v0.5.0`

Repo: **https://github.com/minghao51/evalops** (public) · branch `main` · CI green on 3.10/3.11/3.12
(tests: **482 offline + 1 Postgres contract test, deselected**).
Read this file, then `docs/CONTRACT.md` (the invariants) and `docs/IMPLEMENTATION_MAP.md` (the plan).
`docs/STATE.md` is the short status board; phases 1 and 2 are closed (**G1**, **G2**), and phase 3b
(judges, native-first) is **WP-complete**: WP3b.1 `JudgeSpec`, WP3b.2 (native adapters), WP3b.3
(LiteLLM fallback + wiring), WP3b.4 (pairwise/panel wrappers) and **WP3b.5** (pin enforcement +
resolved-model recording + nightly settings check + calibration as gate input, `e484552`) all
landed — only the **G3b close** (tag `v0.5.0` after CI green) remains before phase 3. The completed
`docs/packets/WP3b.5.md` / `WP3b.4.md` are the newest packet templates.

## 0. Current snapshot (start here)

| | |
|---|---|
| Verified base | `e484552` (WP3b.5) — CI green on py3.10 / 3.11 / 3.12 (confirm with `gh run list`); this docs commit sits on top |
| Tests | 482 offline (`make check`), standalone 482/482, 1 Postgres contract test (excluded) |
| Phase 1 | **closed at G1** (WP1.1–1.5) |
| Phase 2 | **closed at G2, tagged `v0.4.0`** — WP2.1 `135bf0e` + exec bit `b670d7b`, WP2.3 `684c629`, WP2.2 `86f75fa`, WP2.4 `26d292c`, WP2.5 `4e00b29` |
| Phase 3b | **all WPs done**: 3b.1 `173a416`, 3b.2 `c2beda5`, 3b.3 `f8668fe`, 3b.4 `59518c2`, 3b.5 `e484552` — next: close G3b (tag `v0.5.0` after CI green) |
| Tree | clean; no uncommitted work |
| Next artifact | **G3b close**: checklist (every preset's default judges native + pinned + recorded; fallback only where declared) → tag `v0.5.0`. Then phase 3 (docker + credentials — ask the maintainer) |
| Gate ahead | **G3b** (tag `v0.5.0`): every preset's default judges native + pinned + recorded; fallback only where declared. Enforcement is live since `e484552` |

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
  trace model v2 (WP1.5). Phase 2 is closed at G2 (`v0.4.0`): `evalops pipeline` (WP2.1), release
  re-verification (2.2), presets + capability matrix (2.3), the four generated CI adapters (2.4) and
  calibration enforcement (2.5). Phase 3b (judges, native-first) is nearly done: `JudgeSpec`
  (3b.1), the native judge adapters (3b.2), the LiteLLM fallback + spec→scorer wiring (3b.3) and
  the pairwise/panel wrappers (3b.4) are in, and pin enforcement (3b.5) landed (`e484552`) — only the **G3b**
  tags `v0.5.0`.

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
| `codex-luna-reviewer` (user scope, `~/.pi/agent/agents/`) | `openai-codex/gpt-6-luna` @ **xhigh** | adversarial review: runs commands, falsifies claims, PASS/BLOCK — **the current verified route** (WP2.4, WP3b.1–3b.4) |
| `evalops-reviewer` (project scope) | `zai/glm-5.3-flash` @ high | previous reviewer route (WP1.x–WP2.3); fine when zai has quota |

Fallback, verified 2026-10-24: `opencode-go/deepseek-v4.1-flash` (`OPENCODE_API_KEY`) runs the
worker/scout role when the official deepseek key is unavailable — same model, different gateway;
both routes were dispatched through the real agent harness and produced equivalent smoke reports
(one agent-level A/B dispatch per route). `deepseek/deepseek-flash` **is** DeepSeek-V4.1-Flash on
the official API (`GET /models`), so the worker model name is stable across both gateways. A second
reviewer fallback exists: `evalops-reviewer-alt` (user scope, `opencode-go/glm-5.3`, high) — used
for the WP2.4 cross-check and WP2.5 when zai's quota ran out; both runs PASS, 0 defects.

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
   missing exec bit → `b670d7b`). Local side branches `wp12-alternative-33ec945`,
   `wp21-alternative-fe6a1c5`, `wp22-alternative-ad5db19` and `crosscheck/wp2.4-mine` exist on this
   machine as references.
4. **Stay in disjoint trees when racing:** if their docs commit says "WP3b.2 is next", take
   WP3b.3 (or the next untouched WP) instead (the project's own rule: two parallel workers only in
   disjoint trees). Phase 3b's WPs touch the same files (`config/models.py`, `scoring.py`,
   `scorers/judges.py`, `run.py`), so **serialize** them unless a packet's allowed paths are
   provably disjoint; do not race two workers inside 3b.
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
| WP2.4 CI adapters (parallel session) + cross-check of an independent duplicate | 1 BLOCK (6 defects) + narrow re-review PASS; the duplicate found no blocking defect (the fork-guard removal is a documented, reviewed decision — see §6) | 6 |
| WP2.5 calibration enforcement | 1 (PASS, 0 defects; reviewer rebuilt the fixture, re-ran the pr/main failing and passing runs, and all 4 mutations) | 0 |
| WP3b.1 JudgeSpec | 2 BLOCK + 1 orchestrator adjudication + narrow PASS (`Infinity` in the envelope + `OverflowError`; parser digit-limit exit 5 + un-normalised int bounds; the layer-naming point was adjudicated, not a defect — the test now pins the exact per-layer path) | 7 |
| WP3b.2 native judge adapters | 1 (PASS, 0 defects; the worker's two declared deviations — pyright-ignores on the lazy imports, VERIFY-marked pass-through kwargs — reviewed and accepted) | 0 |
| WP3b.3 LiteLLM fallback + wiring | 1 mid-WP orchestrator adjudication (H5 blocked: the core evaluator's usage collector did not cover scoring — allowed paths extended by a narrow core.py change) + 1 BLOCK (3 defects: `NaN` escaping the strict parser, rubric echo in error evidence, docstring leaking into the schema diff) + narrow fix + narrow re-review PASS (one adjudication: the untracked packet file = orchestrator artifact) | 3 |
| WP3b.4 pairwise/panel wrappers | 1 BLOCK (2 construction-validation defects: generators accepted as a panel; unhashable method raising raw `TypeError`) + narrow fix + narrow re-review PASS | 2 |

Reviewer route: `evalops-reviewer` pins `zai/glm-5.3-flash`. On 2026-10-24 zai's 5-hour quota was
exhausted mid-session, so the WP2.4 cross-check and WP2.5 reviews ran on a substitute
(`opencode-go/glm-5.3`, thinking high) registered as a **user-scope** agent file
`~/.pi/agent/agents/evalops-reviewer-alt.md` (a copy of `.pi/agents/evalops-reviewer.md` with
`name:`/`model:`/`description:` changed — the description must be quoted YAML or the file fails to
load). Both substitute reviews returned PASS with 0 defects and re-ran the mutations independently,
so the route is acceptable when zai is unavailable; record the substitution in the commit body.

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
| 2.4 CI adapters from one template (all four generated, drift + no-logic tested) | done | `26d292c` (1 BLOCK: 6 defects + narrow re-review PASS) |
| 2.5 calibration enforcement (real `pr`/`main` pins + failing checks named in the stage error) | done | `4e00b29` (1 review round: PASS, 0 defects) |

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

## 6. Phase status — phases 1 and 2 closed (G1, G2 at `v0.4.0`); phase 3b in progress

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

**Cross-check:** this session built an independent WP2.4 in parallel (`ci_adapters.py` with
`adapters_text`/`make adapters`, a ≤5-entry plumbing allowlist, and the GitHub fork guard kept) and
parked it on local branch `crosscheck/wp2.4-mine`. Auditing the landed `26d292c` against it found
nothing blocking: their drift probe goes red on a mutated byte, a stage appended to `STAGES` raises
`KeyError … add it to BINDINGS`, the shim is byte-identical, and behaviour parity holds. Two
differences worth knowing: (a) **fork PRs now run** — the `head.repo` guard is gone by design, the
pr job checks out `refs/pull/<N>/head` and WP2.2 routes state to the bot branch, and the adapter
comment documents the write-token repository setting; the residual risk is that untrusted fork code
now executes under the workflow's permissions (the `id-token: write` grant remains), so the
maintainer should confirm that posture rather than inherit it silently; (b) `evalops ci-adapters`
is a deterministic command with no envelope golden while `capabilities` has one — a small
contract-discipline gap, recorded rather than fixed here.

### WP2.5 — calibration enforcement in the pipeline (DONE, `4e00b29`)

The characterisation came first: a below-threshold calibration **already** failed `pipeline pr`
(WP2.1's stage table + per-step exit propagation) — exit 1 at `calibrate`, no plan/eval/gate, no
eval run row, nothing promoted — so no plumbing was rebuilt. What was missing was why: the message
was `…: calibrate reported a failure`. `_step_detail` now appends the failing checks
(`; failing checks: helpful: agreement >= 0.99 (measured 0.0)`), mined from the nested payload
(`data.checks` with a string `check` + `passed: false` + optional `candidate`), discriminated by
`data.judges` — gate's checks share the entry shape, so a shape-only key would have changed every
gate failure message; gate messages stay byte-identical. The pins are the substance: real `pr` and
`main` runs below threshold exit 1 at `calibrate` with no eval row and nothing promoted, a passing
calibration proceeds, a missing calibration dataset stays a **skip** (decision pinned; the
pre-existing dry-run pin is unmodified), and four mutations (step removed, calibrate forced green,
clause removed, `data.judges` discriminator removed) each turn the named test red. 353 → 359 tests.
Residual: the `data.judges` discriminator is a negative-space property ("no other command emits
`judges` today") — worth one sentence in `docs/CONTRACT.md` when WP3b.5 makes calibration a gate
input; that CONTRACT line now distinguishes the two enforcement points (the pipeline stage enforces
today; the gate-input half is WP3b.5), and README's CI paragraph names Jenkins + `make ci-adapters`.

### G2 gate (phase 2 exit — CLOSED 2026-10-24, tagged `v0.4.0`)

Every checklist item holds; evidence:
- **Pipeline parity:** the four stage tables are the four bash stages, step for step, and
  `pipeline <stage> --dry-run` lists them and writes nothing (fresh fixture project, all exit 0):
  `pr` = `guard, sync, commit_state, calibrate, plan, eval, gate`;
  `main` = `guard, sync, calibrate, plan, eval, gate, promote, commit_state`;
  `release` = `guard, gate, baseline, promote, commit_state`;
  `nightly` = `baseline, checkout, eval, gate, restore`. Real runs are pinned by WP2.1/2.2/2.5
  (`tests/unit/test_pipeline.py`, `test_release_reverify.py`, `test_calibration_enforcement.py`).
- **Seven presets:** `evalops init --preset <p>` exits 0 for `local, mlflow, databricks, langfuse,
  langsmith, logfire, mixed`; `evalops capabilities` is deterministic and
  `make capabilities` leaves zero drift (`tests/unit/test_presets.py` re-renders all seven).
- **Calibration enforced:** a real below-threshold calibration fails `pr` and `main` at the
  `calibrate` step (exit 1, no eval row, nothing promoted) and the stage error names the judge,
  metric, required limit and measured value; absence stays a skip. Six pins in
  `tests/unit/test_calibration_enforcement.py`, four mutations proven red.
- **Gates green:** `make check` **359**, standalone **359/359**, `make contract` 1 skipped (docker
  present; the contract test self-skips offline), `make schema` / `make capabilities` /
  `make ci-adapters` zero drift, goldens untouched. CI `40102918591` (WP2.5) and `40103336121`
  (docs) green on py3.10/3.11/3.12.
- **Tag:** `v0.4.0` on the checklist commit (D10). No code change was needed.

**Next:** phase **3b** (judges, native-first) — WP3b.1–3b.4 are done (see their DONE blocks below
and [`docs/STATE.md`](docs/STATE.md)); only **WP3b.5** remains before **G3b** tags `v0.5.0`.
`docs/CONTRACT.md` §6 already states the target contract; phase 3 (vendor contract verification)
needs services/credentials and stays after 3b (D6).

### WP3b.1 — `JudgeSpec` in core, pin-validated at load (DONE, `173a416`)

`JudgeSpec` is a core, vendor-free value object (`src/evalops/judges.py`): rubric, scale
(`binary`/`ordinal`/`continuous`), pinned `model`, `prompt_version` and sampling, in frozen
dataclasses. A `judges.<name>` rule declares a spec **iff** it carries
`rubric`/`scale`/`prompt_version`/`sampling`; threshold-only rules — the shipped `evalops.yaml`
shape — declare none and behave byte-identically, so this WP deliberately does **not** pre-empt the
3b.5 gate enforcement. An unpinned declared spec is exit 2 naming the dotted config path (model
absent/empty/`latest`/`*-latest`/`auto`/`default`; prompt_version absent/empty/`latest`; empty
rubric), as are bad scale/sampling values and nested unknown keys. `plan`/`evaluate`/`calibrate`
load the specs before any estimate, budget reservation or ledger write, so an invalid spec costs
nothing and writes no row. `evalops judges --json` exposes the identity surface (`rubric_hash`,
never the rubric text) plus an `unpinned` worklist; the config schema gains the four judge keys and
`docs/evalops.schema.json` (plus the schema-embedding `config_schema` golden) is regenerated.

Numeric validation is deliberately two-layered, both exit 2: the pydantic `JudgeNumber` rejects
bool/±inf/NaN at the exact key (so `config validate` can never accept a value that serialises as
`Infinity`), and `JudgeSpec.validate()` rejects the same plus float overflow for direct API use;
a YAML integer above CPython's 4300-digit limit is translated by the config loader like every other
malformed yaml (exit 2 naming the file, CONTRACT §2). 29 new tests (359 → 388). Review: 2 BLOCK
rounds — `Infinity` in the envelope plus an `OverflowError`, then the parser digit limit escaping
as exit 5 and un-normalised int bounds — an orchestrator adjudication (which validation layer names
the path is not a requirement; the test now pins the exact per-layer path), and a final PASS.

**Still open and deliberately deferred to 3b.5:** a `model` returned by `judge_meta()` may still
disagree with `judges.<name>.model` (CONTRACT §6 says that conflict is exit 2 at load and that every
judge, including a `model`-only rule, is pin-validated); `unpinned` is not an exhaustive list.
Recorded as STATE compromise 17.

### WP3b.2 — native judge adapters per preset (DONE, `c2beda5`)

`src/evalops/scorers/native.py` declares the four-entry `ADAPTERS` registry — exactly one native
judge adapter per evaluator backend: MLflow `make_judge` (`mlflow`, which also serves the
`databricks` preset), Langfuse managed evaluators with server-side execution + score read-back
(`langfuse`; deterministic config key of judge name + `rubric_hash` + prompt_version), openevals
(`langsmith`) and pydantic-evals (`core`, which serves `local`/`logfire`/`mixed`). Every SDK import
is function-local (the guard-exempt lazy pattern — the guard now also flags module-level
`openevals`/`pydantic_evals` imports, each with a planted probe) and an `ImportError` becomes
`CapabilityError` → exit 6 naming the pip package (CONTRACT §3.0/§7.4); an evaluator with no
adapter is exit 6 too. Each factory returns a `call_scorer`-compatible neutral scorer (name =
`spec.name`, `llm=True`) that normalizes through `to_float` and turns a missing vendor score into
`None`, never `0.0`; vendor kwarg/route details are `VERIFY`-marked like the plugins (26 markers,
phase 3 resolves them). `scorers/judges.py` gains `spec_judge` as the wrapper over the registry;
`span_judge`/`trajectory_judge` are byte-identical. The capability matrix gains a generated
native-judges section (payload key + docs table; the presets column is derived from `PRESETS`,
never restated); `capabilities.json` is the only golden change. Deliberately **no**
`plan`/`evaluate`/`calibrate` wiring: auto-selecting a scorer from a declared `JudgeSpec` needs
the LiteLLM fallback (3b.3) so every spec resolves; resolved-model recording + enforcement are
3b.5. 12 new tests (388 → 400). Review: 1 round PASS, 0 defects (the worker's two declared
deviations — pyright-ignore comments on the four lazy imports, and VERIFY-marked pass-through of
`prompt_version`/sampling kwargs the vendors do not document — were reviewed and accepted).

### WP3b.3 — LiteLLM rubric fallback + spec→scorer wiring (DONE, `f8668fe`)

A declared `JudgeSpec` now resolves to a real scorer. `judges.<name>.judge` selects the executor —
absent/`native` uses the evaluator's WP3b.2 native adapter, `litellm` the evalops rubric fallback
(`scorers/litellm_judge.py`); the key is a closed set validated at both layers (config model at the
exact key, `JudgeSpec.validate()` at the dotted path, both exit 2) and never declares a spec by
itself, so threshold-only rules are byte-identical. The fallback passes the full spec to
`litellm.completion` (pinned model, sampling, JSON mode) behind a function-local import; a missing
SDK is `CapabilityError` exit 6 naming `pip install litellm`. Parsing is strict:
`NaN`/`Infinity` (non-standard JSON constants), unrepresentable numeric literals and non-number
scores are judge-named exit-5 errors — never `0.0`, never `None` — and out-of-range scores clamp to
the declared scale. Error evidence is rubric-checked: an echoing model or a provider exception
cannot leak rubric text into `r.error`. Token usage is reported per call (`report_usage`), and the
core evaluator now opens its per-row usage collector around **scoring** too (it wrapped only
`predict` before, so scorer-reported tokens never reached `row.usage`) and merges additively with
app-reported usage keeping precedence over the tracer backfill; projects without declared specs are
byte-identical (the eval golden still pins 210/50). `plan`/`evaluate` count one client-side call
per row per declared spec in the estimate (`NativeJudge.server_side`: only `langfuse-managed` is
`True`); a name collision between an app scorer and a declared spec is exit 2 before any ledger
row; `calibrate` now yields real agreement/kappa/MAE pairs for declared specs. First `pyproject`
change since phase 2: the `litellm` extra (no preset/plugin references it; doctor pins untouched).
Gate-time pin enforcement, resolved-model recording and the nightly settings check remain 3b.5;
pairwise/panel is 3b.4. 33 new tests (400 → 433). Review: 1 BLOCK round — 3 defects (`NaN`
escaping the strict parser via `json.loads`' default constants; rubric echo reaching error
evidence; a docstring leaking into the generated schema diff) — then a narrow fix and a narrow
re-review that reproduced all three fixes' mutations red. The re-review's remaining item (the
untracked packet file in the tree) was adjudicated: the packet is the orchestrator-owned dispatch
artifact and lands in the docs commit (WP3b.1/WP3b.2 convention). The H5 blocker mid-WP (scorer
usage unreachable within the original paths) was adjudicated by extending the allowed paths to the
narrow core.py scoring-collector change.

### WP3b.4 — pairwise + panel wrappers (DONE, `59518c2`)

`src/evalops/scorers/panel.py` is the map's comparison layer as app-hook composables (pure Python,
vendor-free; same shape as `span_judge`).
`normalize_pairwise` maps a verdict to `a`/`b`/`tie` from a closed set (case-insensitive strings,
`1`/`-1`/`0`, a `{'winner': ...}` mapping) with a `swapped` frame-flip; unknown values are
wrapper-named exit-5 errors, never a silent tie. `pairwise_judge` calls the judge in **both
orders** per row and emits the decision plus `name/swap_agreement` — a position-biased judge shows
up as `0.0`, never smoothed; swap disagreement lands on the visible `0.5` (missing baseline names
the key before the judge is called). `panel_judge` aggregates ≥2 members by mean, strict majority
(no majority → visible `0.5`; falsy majorities handled), or exact agreement (no consensus →
`None`), and always emits `name/panel_disagreement` (`1 −` largest vote share) when votes exist;
`None` votes are excluded, multi-metric members are rejected by index, and members/method are
validated at construction with exit 2 (generators, strings/bytes and unhashable methods included —
the re-review's two defects, both mutation-proven). No config surface, no run.py/gate wiring;
cost documented (2 calls/row pairwise, one per member/row panel — `judge_meta.llm_judge_count`
must account for them). 21 new tests (433 → 454). Review: 1 BLOCK round (2 construction-validation
defects) + narrow fix + narrow re-review PASS.

### WP3b.5 — pin enforcement + resolved model + nightly settings check + calibration as gate input (DONE, `e484552`)

CONTRACT §6's enforcement bullets are live: every `judges.<name>` rule without a concrete pin fails
the gate (exit 1, `judge pin: <name>` check; no-judges configs byte-identical — gate/eval/plan/
traces goldens unmoved); a `judge_meta()` identity (`judge_model`/`judge_prompt_version`)
disagreeing with any declared spec is exit 2 at load at plan/evaluate/calibrate (never at
`evalops judges`); every declared-spec verdict records `judge_models[name]` in `rows.jsonl`
(additive) plus `judge.<name>.model`/`.prompt_version` run params, and an identity-less verdict is
the judge-named exit 5; the gate consumes the judge's latest calibration evidence (`evalops.kind:
calibration`, latest by start_ms+run_id) against the config's limits — failing evidence is a failed
`judge calibration: <name>` check, absence still emits nothing (STATE 14 intact); `unpinned` is the
exhaustive refusal worklist (STATE 17 resolved; `judges.json` golden +1 line, the WP's only golden
churn); `evalops judges --check-settings` compares declared pins to the Langfuse project default
and the pydantic-evals default (fake-SDK offline, VERIFY-marked, exit 1 on drift, 6 on missing
SDK), and the nightly stage runs it after `baseline`, before checkout. Deliberate behavior changes
(fixture configs gained the pins their `judge_meta` already claimed): test_calibration_enforcement,
test_pipeline, test_e2e_agent, test_litellm_judge. 28 new tests (454 → 482). Review: 1 PASS
(codex-luna-reviewer, gpt-6-luna xhigh) + 1 narrow mutation round (all six C7 mutations red,
golden byte-identity re-proven).

After 3b.1 → 3b.2 (native adapters per preset) → 3b.3 (LiteLLM fallback) → 3b.4 (pairwise/panel) →
3b.5 (pin enforcement + resolved model + nightly settings check + calibration as gate input) →
**G3b** tags `v0.5.0` (the LiteLLM optional dep and fake SDK fixtures already landed in 3b.3;
stays offline).

## 7. Remaining roadmap (phases 2 → 6)

| Phase | WPs | Deliverable | Gate / needs |
|---|---|---|---|
| **2** CLI/pipeline as code + presets — **closed at G2, tagged `v0.4.0`** | 2.1 `evalops pipeline <stage>` in Python (`--dry-run`, idempotent, bash shim) **done**; 2.2 release re-verification + fork-PR locks + nightly fails without baseline **done**; 2.3 preset engine + `evalops init --preset` (7 presets) + generated capability matrix **done**; 2.4 CI adapters generated from one template (GH/GitLab/Azure/Jenkins) **done**; 2.5 calibration enforced in the pipeline **done** | pipeline parity, 7 presets, calibrate enforced | **G2**: tag **v0.4.0**. No external services. |
| **3b** judges, native-first — 3b.1–3b.4 **done** | 3b.1 `JudgeSpec` (pin required, unpinned declared spec = exit 2 at load, `evalops judges`) **done** `173a416`; 3b.2 native adapters (MLflow `make_judge`, Langfuse managed evaluators, openevals, pydantic-evals) **done** `c2beda5`; 3b.3 LiteLLM rubric fallback + strict parsing + per-call cost + spec→scorer wiring (`judges.<name>.judge`) **done** `f8668fe`; 3b.4 pairwise (position swap) + panel **done** `59518c2`; 3b.5 pin enforcement (incl. `judge_meta()` never overriding the pin, model-only rules), resolved model per verdict, nightly server-side settings check, calibration as gate input **done** `e484552` | every preset's default judges native + pinned + recorded | **G3b**: tag **v0.5.0**. The LiteLLM optional dep + fake SDK fixtures are in (3b.3); stays offline. |
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
| **Fork-PR posture (WP2.4)** | the GitHub `head.repo` guard is gone by design: fork PRs now run the `pr` stage (checkout `refs/pull/<N>/head`, state to the WP2.2 bot branch) with the workflow's permissions — including `id-token: write` — and the write-token repository prerequisite is documented in the adapter | **maintainer to confirm** the posture, or restore a same-repo-only guard in `ci_adapters.py` (one spec/renderer change + adapter regen) |
| **`ci-adapters` envelope golden** | `evalops ci-adapters` is deterministic but has no `tests/fixtures/envelope/` golden while `capabilities` does | small follow-up: add the golden (`EVALOPS_UPDATE_GOLDENS=1`) + name it in CONTRACT's snapshotted list, or record the exemption |

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
  judges.py              JudgeSpec (rubric/scale/pin/prompt_version/sampling) validated at load;
                         `evalops judges` identity surface + the `unpinned` worklist
  ci_adapters.py         the four CI adapters rendered from one per-stage spec (make ci-adapters)
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
cd /home/howt/work/evalops_universal/evalops && make check      # expect 482 passed, 1 deselected
git fetch origin && git log --oneline main..origin/main     # is the other session ahead? which WP?
gh run list --limit 1                                       # confirm CI green on main
git tag -l v0.4.0                                           # phase 2 close marker (v0.5.0 = G3b)
sed -n '/### Phase 3b/,/### Phase 3 —/p' docs/IMPLEMENTATION_MAP.md
sed -n '/### WP3b.5/,/^## 7/p' HANDOFF.md                   # the WP that just landed
```

Then:

1. **Check §4's coordination rules first.** `git fetch` + `git log --oneline main..origin/main`:
   if the other session has already closed G3b or started phase 3, cross-check before doing
   anything (disjoint trees or take the next WP).
2. **Close G3b** — phase 3b is WP-complete (WP3b.5 at `e484552`). The gate checklist: every
   preset's default judges native + pinned + recorded; fallback only where declared. Concretely:
   the capability matrix lists a native adapter per evaluator (3b.2), every enforcement surface is
   live (`judge pin:`/`judge calibration:` gate checks, exhaustive `unpinned`, `--check-settings`,
   per-verdict `judge_models`), and `make check` + the zero-diff gates are green. Then tag
   **`v0.5.0`** on main after CI is green.
3. **Phase 3 (vendor contract verification) needs docker + LangSmith/Logfire/Databricks
   credentials — ask the maintainer before starting it.** Two maintainer confirmations are still
   pending (§8): the WP2.4 fork-PR posture and the missing `evalops ci-adapters` envelope golden.

Suggested opening message for the new thread (paste as the first user message):

```text
Read HANDOFF.md (§0 snapshot, §2 non-negotiables, §4 dispatch protocol + coordination, §6 WP3b.5
record, §8 open decisions, §10), docs/CONTRACT.md (§1 exit codes, §3 port invariants, §6 judge
pinning — enforced since WP3b.5), docs/STATE.md (compromises 14/17 as resolved), and the map's
phase-3b row + G3b gate in docs/IMPLEMENTATION_MAP.md. Phase 3b (judges, native-first) is
WP-complete: WP3b.1 (JudgeSpec) at 173a416, WP3b.2 (native adapters) at c2beda5, WP3b.3 (LiteLLM
fallback + wiring) at f8668fe, WP3b.4 (pairwise/panel wrappers) at 59518c2, and WP3b.5 (pin
enforcement + resolved-model-per-verdict + nightly settings check + calibration as gate input) at
e484552 — 482 offline tests green (make check), CI green on py3.10/3.11/3.12, phase 2 closed at G2
(tag v0.4.0). A second agent session may work this repo — follow HANDOFF §4's coordination rules:
git fetch + git log --oneline main..origin/main before dispatching anything, fetch/rebase before
every commit, never reimplement a WP that already landed (branch a duplicate as
wp<N>-alternative-<sha>, cross-check, fix narrow gaps only), stay in disjoint trees.

First close G3b: verify the gate checklist (every preset's default judges native + pinned +
recorded; fallback only where declared — the capability matrix + the live enforcement surfaces:
judge pin/calibration gate checks, the exhaustive `unpinned` worklist, `evalops judges
--check-settings`, per-verdict judge_models), run the full gate set (make check,
tests/run_without_pytest.py, make contract, make schema/capabilities/ci-adapters zero diff), and
tag v0.5.0 on main after CI is green.

Then STOP before phase 3 (vendor contract verification): it needs docker + LangSmith/Logfire/
Databricks credentials — ask the maintainer. Also pending maintainer confirmation (HANDOFF §8):
the WP2.4 fork-PR posture and the missing `evalops ci-adapters` envelope golden.

Protocol for any new WP: write docs/packets/<WP>.md in the shape of docs/packets/WP3b.5.md (goal,
fixed design decisions, falsifiable done-when, allowed/forbidden paths, commands, report format,
reviewer prompt); dispatch it to evalops-worker (deepseek/deepseek-flash, cwd =
/home/howt/work/evalops_universal/evalops); review the uncommitted tree with codex-luna-reviewer
(openai-codex/gpt-6-luna, thinking xhigh; fallbacks: evalops-reviewer zai/glm-5.3-flash high, or
evalops-reviewer-alt opencode-go/glm-5.3 — say which you used); loop on BLOCK; commit only after
PASS as a feature commit + a separate docs commit; fetch/rebase before every commit; push and
confirm with gh run list. Gates before every commit: make check, .venv/bin/python
tests/run_without_pytest.py, make contract (skips offline), make schema zero drift, make
capabilities and make ci-adapters zero diff, no unexpected golden churn. Never fabricate hashes,
numbers, or evidence; undefined contract semantics are blockers — ask the maintainer.
```

