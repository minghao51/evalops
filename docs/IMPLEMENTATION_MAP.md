# evalops — Implementation Map

Status: **authoritative plan** (reconstructed from the v0.3 HANDOFF + code audit on 2026-10-23).
Owner: orchestrator. Workers and reviewers treat this file and `docs/CONTRACT.md` as the contract.
Nothing in this file is changed by a work package (WP); changes require an orchestrator decision entry.

Baseline: `evalops` v0.3.0, 17 tests green (`python tests/run_without_pytest.py`).
Target: v1.0.0 publishable package, 7 presets, verified vendor plugins, pinned+calibrated judges,
Copier template, real-framework trace fixtures.

---

## 0. Strategy

**Wrap and normalize, never rebuild.** evalops delegates execution, tracing, storage, UIs and model
calls to the backend. It owns only the glue:

1. version pinning (prompts, datasets, behavior config, judges),
2. the plugin contract (5 roles) and its conformance suite,
3. normalization of heterogeneous agent traces into one model,
4. statistics and gates (pass^k, CI bounds, cost, drift),
5. the CLI contract + pipeline (stable exit codes, `--json`, one implementation),
6. safety (redaction, budget, breaker) and reproducibility (cassettes).

**Native judges first.** Each preset wraps the judges its backend already has (MLflow `make_judge`,
Langfuse managed evaluators, openevals for LangSmith, pydantic-evals for Logfire/local). The evalops
rubric judge over LiteLLM is a fallback, not the default. Pairwise and panel judges are wrappers
around whichever native judge is in use.

**Every judge is pinned, and the resolved model is recorded per verdict.** A gate refuses a judge
running on a default/alias model.

**Additive, versioned persistence.** Every persisted artifact carries `schema_version`; readers accept
the previous version for one minor release. Identity hashes (dataset fingerprint, cassette
fingerprint, `app_version`) are computed over **semantic fields only**, never over metadata, so
adding `schema_version` does not invalidate every recorded run.

---

## 1. Decisions

Numbered; ADRs mirror them in `docs/adr/`. Changing one needs an orchestrator entry here.

| ID | Decision | Resolution | ADR |
|---|---|---|---|
| D1 | Package name | Keep import package `evalops` for now. **Do not publish as `evalops`**: PyPI `evalops` is squatted by an unrelated project, and the GitHub org `evalops` + PyPI `evalops-sdk` belong to "Deixic, Inc." (a live EvalOps product). **Distribution name decided 2026-10-24: `evalplane`** (ADR 0001's first preference; free on PyPI + GitHub org at the 2026-10-23 check). Publication stays in WP6.1, which re-verifies availability before publishing and falls back to `agentgates`, then `evalops-core`, only if taken meanwhile. Distribution name is decoupled from import name. | 0001 |
| D2 | License | Apache-2.0 (patent grant; matches the CI/plugin ecosystem) | 0002 |
| D3 | Repo | `github.com/minghao51/<repo>`, private until G4, then public. No pushes without explicit instruction. | — |
| D4 | Pydantic in core | Yes: pydantic v2 for config + generated JSON Schema. Core stays **vendor-free**, not dependency-free. The `ports` layer stays dataclass-based (no pydantic in plugins/scorers). | 0003 |
| D5 | Plan source of truth | This file (reconstruction). If the original Claude Doc export becomes available, reconcile as a diff review, not a rewrite. | — |
| D6 | Phase order | 0 → 1 → 2 → 3b → 3 → 4 → 5 → 6. **3b before 3**: judge adapters are pure code and unblock the gate; phase 3 needs live services and credentials. | 0004 |
| D7 | Contract testing now | docker-local only: MLflow+Postgres, Langfuse+Clickhouse. LangSmith / Logfire / Databricks stay `beta` behind user credentials; recorded fixtures keep CI offline-green. | 0005 |
| D8 | Presets (7) | `local, mlflow, databricks, langfuse, langsmith, logfire, mixed`. `mixed` = the composition preset (any role can point anywhere). | — |
| D9 | Python floor | `>=3.10`, CI matrix 3.10/3.11/3.12 | — |
| D10 | Release cadence | v0.4.0 at G2 (formats+redaction+pipeline), v0.5.0 at G3b (judges), v1.0.0 at G6 | — |
| D11 | Judge pinning source | `judges.<name>.model` in `evalops.yaml` is the pin; `judge_meta()` may only add, not override. Unpinned → gate fails (not a warning). | 0006 |
| D12 | Exit-code contract | 0 ok · 1 gate/calibration failed · 2 usage/config · 3 budget · 4 guard · 5 runtime · 6 unsupported by plugin. Never renumbered. **7 = contract/format mismatch** (new, WP0.4). | 0007 |

---

## 2. Work packages

Rules that apply to every WP:

- One WP = one worker dispatch = one review = one commit.
- Done-when is falsifiable and stated in the dispatch; "tests pass" alone is never done-when.
- The 17 baseline behaviours stay green; a WP that must change a baseline assertion states why.
- New public surface needs a test that fails without it.
- No new vendor dependency outside `src/evalops/plugins/`.
- Persisted-format changes ship with: version bump, v(n-1) read test, and fingerprint-invariance test.

### Phase 0 — Bedrock: contract, config, versions
| WP | Deliverable | Done-when | Owner |
|---|---|---|---|
| 0.0 | repo, venv, agent roster, this map | committed baseline; agents defined | orchestrator |
| 0.1 | `docs/CONTRACT.md` (exit codes, JSON envelope v1, port contracts, format policy, compat) + ADRs 0001–0007 | every decision D1–D12 has an ADR; contract doc states the invariants a plugin must keep | worker A |
| 0.2 | Dev tooling + test layout: `tests/{unit,integration}`, pytest markers (`contract`, `slow`), ruff, mypy (strict on `contract/config/stats`), `make check`, `.github/workflows/ci.yml` (3.10–3.12), no-vendor-import test | `make check` green from a clean venv; 17 tests still discovered after the move | worker A |
| 0.3 | **Typed config**: pydantic v2 models for `evalops.yaml`, generated `docs/evalops.schema.json`, `evalops config validate\|schema\|explain`, legacy `Config` shim | schema validates the shipped yaml; unknown key → exit 2 naming the key path; `EVALOPS_ENV` merge semantics preserved (test) | worker |
| 0.4 | **Format versioning**: `FORMAT_VERSION=1` in locks/rows/cassettes/last_eval/artifact manifest; v0 read-compat; semantic-only fingerprints; `evalops_version` in ledger params/tags | v0 fixture parses; v1 round-trips; cassette fingerprint and `app_version` unchanged by metadata; exit 7 on unknown future version | worker B |
| 0.5 | Envelope + exit-code freeze: `{ok, code, schema_version, command, env, data, errors}`; golden snapshots for all commands; `evalops --version` | exit matrix 0–7 tested; golden JSON stable across backends | worker |
| 0.6 | `docs/STATE.md` + `CHANGELOG.md` + `make check` CI badge | state file lists WP status + next WP | orchestrator |
| **G0** | | contract frozen; formats versioned; `make check` green on 3.10–3.12; no vendor import in core | |

### Phase 1 — Kernel hardening & safety
| WP | Deliverable | Done-when |
|---|---|---|
| 1.1 | Redaction: rule list (field path / regex / hash / callback), applied at the 3 outbound boundaries (ledger artifacts, trace `to_dict`, cassettes), after identity hashing; `evalops redact --check`; `EVALOPS_REDACT=strict\|off` | test proves a secret never reaches any sink while `app_version` and fingerprints are unchanged; idempotence property test |
| 1.2 | Ledger integrity: Postgres in docker, `evalops ledger migrate\|verify`, atomic budget reserve/settle, UTC month boundary, N+1 fix in `find_runs` | concurrent budget test cannot overspend; verify detects tampering |
| 1.3 | Statistics: one stdev convention, Wilson + bootstrap CI, exact `pass^k` semantics, **stable row identity** (dataset fingerprint + row key, not index), baseline pinned by group+hash, real drift math, change attribution in the gate report | property tests: pass^k monotone in k; CI shrinks with n; reordered dataset keeps row alignment; drift report names the changed param |
| 1.4 | Test debt: unit tests for `gate.reliability/ci_lower/check_rows`, cassettes (record/replay/miss/fingerprint), `calibrate._stats`, `evalops traces`, `promote --candidate/staging`, `rollback`, `plan` budget failure | coverage of every exit-code path; no gate branch untested |
| 1.5 | Trace model v2 + capture hardening: `source`, `schema_version`, unknown-attribute allowlist, breaker parity notes | `AgentTrace` round-trips v1 and v2; unknown attrs reported, not silently dropped |
| **G1** | | redaction proven; formats verified; stats property-tested; every exit path tested |

### Phase 2 — CLI/pipeline as code + presets
| WP | Deliverable | Done-when |
|---|---|---|
| 2.1 | `evalops pipeline <stage>` in Python (pr/main/release/nightly) with declarative stage table, `--dry-run`, idempotence; `ci/pipeline.sh` = shim | pipeline reproduces the bash stages in a fixture repo; dry-run lists every command it would run |
| 2.2 | Release re-verification (guard+gate+baseline+evidence-group→version binding); fork-PR lock handling via bot branch; nightly fails loudly without baseline | release refuses an evidence group that never passed the gate; fork PR path tested |
| 2.3 | Preset engine + `evalops init --preset <p>` for the 7 presets; capability matrix generated from code | each preset's doctor expectations are asserted; matrix regenerates with zero diff |
| 2.4 | CI adapters (GH/GitLab/Azure/Jenkins) generated from one template; no logic in YAML | all 4 render and call `evalops pipeline`; snapshot test |
| 2.5 | Calibration enforcement in the pipeline | calibration below `judges.min_agreement/min_kappa` fails pr |
| **G2** | | pipeline parity; 7 presets; calibrate enforced; v0.4.0 tagged |

### Phase 3b — Judges: native-first adapters (before 3)
| WP | Deliverable | Done-when |
|---|---|---|
| 3b.1 | `JudgeSpec` (name, rubric, scale, model pin, prompt_version, sampling) in core, no SDK import | spec validates; unpinned spec rejected at load with exit 2 |
| 3b.2 | Adapters: MLflow `make_judge`, Langfuse managed evaluators (server-side + read-back), openevals (LangSmith), pydantic-evals (Logfire/local); `scorers/judges.py` becomes wrappers | each adapter unit-tested against a fake SDK; capability matrix lists which preset has native judges |
| 3b.3 | LiteLLM rubric fallback + strict parsing (JSON mode, scale clamp) + per-call cost | parse failures are errors, never silent 0.0; cost attributed per judge call |
| 3b.4 | Pairwise (position swap) + panel (mean/majority/agreement) wrappers | swap-invariance test: A vs B and B vs A agree after normalization; panel disagreement reported |
| 3b.5 | Pin enforcement + resolved-model recording per verdict + nightly server-side judge settings check + calibration as gate input | **done** — `e484552` (1 PASS review + 1 narrow mutation round; unpinned judge → exit 1 with reason; settings drift fails nightly; calibration evidence blocks the gate; 454 → 482 tests) |
| **G3b** | | every preset's default judges are native, pinned, recorded; fallback only where declared |

### Phase 3 — Vendor contract verification (needs services; keys for langsmith/databricks are backlog)
| WP | Deliverable | Done-when |
|---|---|---|
| 3.0 | Evaluator parity per ADR 0008: universal `error_rate`/`budget_exceeded_rate` emission in `run.py`, `report_usage` + tracer backfill under every evaluator, `doctor` warning for a self-tracing evaluator paired with a separate tracer | vendor evals emit the core metrics; usage lands under vendors (`rows_missing_usage: 0` with `report_usage`); doctor warns honestly; goldens byte-identical. Packet: `docs/packets/WP-PAR.md` |
| 3.1a | Contract slice, mlflow + postgres: compose services in `tests/contract/`, live five-role tests (prompts/datasets/ledger/tracer/evaluator) following `test_ledger_postgres.py`'s pattern | `make contract` green with docker, clean skips without; offline suite untouched. Packet: `docs/packets/WP3.1a.md`. Dispatch AFTER 3.0 |
| 3.1b | Deferred slice: langfuse+clickhouse compose, recorded HTTP fixtures, CI nightly wiring for the contract suite | same green-with-services bar for the langfuse stack; fixtures replay offline in CI |
| 3.2 | Resolve all 30+ `VERIFY` lines, highest risk first: `mlflow.result_df` shape, `langfuse.api.trace.get`, `langsmith.pull_prompt_commit`, logfire private SQL. Evidence already banked from the 2026-10-27 e2e platform run (`../e2e/README.md` outside the repo): logfire `query_json_rows` needs keyword-only `min_timestamp` + `httpx` extra; mlflow 3.17 REST/SDK drift; row-error column mapping | zero unresolved VERIFY: each is an asserted test or a documented `beta` with a user-visible hint |
| 3.3 | Native lineage/backlinks per backend + `evalops lineage` + lineage block in the gate report | a run's lineage is reconstructable from the ledger alone; backlink asserted per backend |
| 3.4 | Generated capability docs + SDK versions in `doctor --json` | docs regenerate with zero diff |
| **G3** | | every VERIFY asserted-green or marked beta; lineage works on ≥2 backends |

### Phase 4 — Copier template & support tiers
| WP | Deliverable | Done-when |
|---|---|---|
| 4.1 | `template/` Copier project (preset, package name, datasets, judge backend, CI provider, redaction policy) | renders a runnable project for all 7 presets |
| 4.2 | Tiers: Generated → Contract-verified → Framework-verified; `evalops doctor --tier` | tier is derived from evidence, not asserted by the template author |
| 4.3 | Golden snapshot test renders all 7 + runs guard/sync/plan/eval/gate offline; `copier update` path | 7/7 lifecycle green; update from a v0.3 template is clean |
| **G4** | | template published; 7/7; repo public |

### Phase 5 — Real framework trace fixtures
| WP | Deliverable | Done-when |
|---|---|---|
| 5.1 | Real span fixtures, pinned versions, `tests/fixtures/spans/<framework>/<version>.json`: LangGraph, OpenAI Agents, ADK, Pydantic AI, CrewAI, LlamaIndex, Strands, AutoGen, Semantic Kernel, smolagents, DSPy | each framework × version has a test that fails if the mapping regresses |
| 5.2 | Dialect table verified + `evalops traces --unknown-attrs` coverage report | unknown attributes are zero or allowlisted, per framework |
| 5.3 | Breaker parity (OpenAI Agents cannot abort → enforce `max_turns`/timeout, documented); cooperative cancel for OTel | runaway-loop test per framework |
| 5.4 | Cassette parity for real frameworks, `record` behind consent | record/replay round-trip per framework |
| **G5** | | 11 frameworks verified; drift visible in CI |

### Phase 6 — Release, docs, ecosystem
| WP | Deliverable | Done-when |
|---|---|---|
| 6.1 | Publish under the D1 name, v1.0.0, changelog, tag→build→matrix→publish workflow | installable from PyPI in a clean env; `evalops doctor` works |
| 6.2 | Docs regeneration (README doctest, PLUGINS generated, AGENT_TRACES, ADR index) | every code block in README executes |
| 6.3 | Plugin SDK: entry-point spec, `evalops plugin new`, conformance kit `evalops plugin test <module>` | two third-party example plugins pass conformance |
| 6.4 | Compatibility policy (semver + FORMAT_VERSION), deprecation table | policy in CONTRACT.md, enforced by tests |
| **G6** | | v1.0.0 published; two external plugins certified |

---

## 3. Testing strategy

| Layer | Tooling | Runs | Rule |
|---|---|---|---|
| Property | Hypothesis | every PR | fingerprints, render, `to_float`, cassette keys, gate monotonicity, redaction idempotence |
| Unit | pytest | every PR, <10 s | offline, zero vendor, no network, no sleeping |
| Integration | pytest | every PR | temp-dir repos: full lifecycle on zero-dep plugins |
| Stateful contract | `RuleBasedStateMachine`, parametrized per backend | nightly + `-m contract` | register-never-overwrites, snapshot immutability, alias sibling preservation, `find_runs` AND + newest-first |
| Vendor contract | pytest + docker-compose + recorded fixtures | nightly | every VERIFY line is a test |
| Span fixtures | pinned real traces | every PR | per framework × version |
| Golden/contract | snapshot JSON | every PR | CLI envelope, exit codes, persisted formats, v(n-1) migration |

Non-negotiables: the offline suite never imports a vendor SDK; a test may not be skipped to go green;
a format change without a migration test is a failed WP.

---

## 4. Risk register

| # | Risk | Mitigation |
|---|---|---|
| R1 | Adding `schema_version` changes fingerprints → every run gets a new `app_version` | semantic-only fingerprints; invariance golden test (WP0.4) |
| R2 | Vendor API drift (30+ VERIFY; logfire private SQL) | contract harness; beta marking; exit 6 with hints; never claim verified without a green test |
| R3 | Server-side judge settings drift (Langfuse project default, pydantic-evals defaults) | resolved model recorded per verdict; nightly settings check; gate refuses unpinned |
| R4 | MLflow local vs Databricks divergence | separate preset + recorded UC fixture |
| R5 | Restructure breaks byte-asserting tests | update assertions inside the WP; never weaken |
| R6 | Renames orphan baselines/ledgers | versioned formats + `ledger verify` + one-minor read-compat |
| R7 | Live credentials unavailable (LangSmith/Logfire/Databricks) | docker-local first; fixtures keep CI green; those presets stay beta |
| R8 | Name/trademark (`evalops` owned by Deixic, Inc.) | D1: publish under a free name |
| R9 | Worker/reviewer disagreement stalls a WP | orchestrator rules; a BLOCK needs a concrete fix, a contested WP gets re-scoped as two WPs |
| R10 | Determinism of e2e tests under parallel CI | no shared tmp paths; fixed seeds; `-p xdist` off for lifecycle tests |

---

## 5. Agent operating model

- **Orchestrator** (this session): owns this map, WP packets, merge, commits, ADRs.
- **Scout** (`deepseek/deepseek-flash`, read-only): recon, VERIFY evidence, fixture capture plans.
- **Worker** (`deepseek/deepseek-flash`, full tools): one WP, tests green, no commits.
- **Reviewer** (`openai-codex/gpt-6-luna`, thinking xhigh, read+bash): falsifies the worker's claims;
  PASS/BLOCK with evidence. Reviewer runs on every WP.

Dispatch protocol: WP packet (goal, allowed paths, forbidden actions, done-when, commands) → worker →
reviewer (WP + diff + claims) → orchestrator commit or re-scope. Writers are serialized per module
family; at most two parallel workers in disjoint trees.

## 6. WP state

Repo: https://github.com/minghao51/evalops (public). CI: GitHub Actions matrix 3.10/3.11/3.12.

| WP | State | Evidence |
|---|---|---|
| 0.0 | done | baseline import `0a538e2`, agents + map `16ce2e1` |
| 0.1 | done | `docs/CONTRACT.md`, ADRs 0001–0007; reviewed |
| 0.2 | done | `tests/{unit,integration}`, recursive runner, ruff/mypy/CI, no-vendor AST guard; reviewed |
| 0.3 | done | pydantic config package, generated schema, `evalops config`; reviewed |
| 0.4 | done | `evalops.contract.formats`, v0 read-compat, semantic fingerprints, exit 7; reviewed |
| 0.5 | done | `src/evalops/envelope.py`, arg/YAML error envelopes, fd-level stdout capture, 17 goldens, exit matrix 0–7; reviewed |
| 0.6 | done | `docs/STATE.md`, `CHANGELOG.md`, first CI run |
| 1.1 redaction | done | `73c5dc7` — 3 outbound boundaries, strict write-path refusal, `redact --check` |
| 1.3 statistics | done | `eb30dd0` — sample stdev, Wilson/bootstrap, exact pass^k, row_key, pinned baseline, drift, attribution |
| 1.2 ledger integrity | done | `2a3ac40` — `ledger migrate\|verify`, atomic budget reservation, UTC month, N+1 |
| 1.4 test debt | done | `1237611` — every exit code 0–7 through real commands; 22 branches mutation-proven |
| 1.5 trace model v2 | done | `8d0c6d9` — nested `schema_version`, unknown attrs by name, breaker parity |
| **G1** | **closed** 2026-10-24 | 283 tests, standalone 283/283, Postgres contract 1 passed, schema/goldens clean; CI `40078618397` |
| 2.1 `evalops pipeline` | done | `135bf0e` + exec-bit `b670d7b` — declarative stage table, `--dry-run`, in-process steps, bash shim |
| 2.3 presets + capability matrix | done | `684c629` — 7 presets, `evalops init --preset`, generated `docs/CAPABILITIES.md` |
| 2.2 release re-verification | done | `86f75fa` — fresh guard+gate+baseline re-check before promote, released-group binding (exit 4 on re-release), fork-PR bot branch `evalops/state/pr-<N>`, nightly fails loudly without a baseline |
| 2.4 CI adapters | done | `26d292c` — one template → GH/GitLab/Azure/Jenkins (Jenkins new), drift + no-logic tests, shim untouched |
| 2.5 calibration enforcement | done | `4e00b29` — real `pr`/`main` stop at a below-threshold `calibrate`; failing judge/metric/limit/measured named in the stage error |
| **G2** | **closed** 2026-10-24, tagged `v0.4.0` (D10) | pipeline parity + 7 presets + calibration enforced; evidence in [`HANDOFF.md`](../HANDOFF.md) §6 |
| 3b.1 `JudgeSpec` | done | `173a416` — rubric/scale/pin/prompt_version/sampling in core; unpinned declared spec = exit 2 at load; `evalops judges` identity surface + `unpinned` worklist; two-layer numeric validation (no `Infinity`/`NaN` in an envelope, no `OverflowError`, oversized YAML ints = exit 2 naming the file) |
| 3b.2 native judge adapters | done | `c2beda5` — one adapter per evaluator (`scorers/native.py` registry: MLflow `make_judge`, Langfuse managed evaluators + read-back, openevals, pydantic-evals), all offline fake-SDK-tested; `ImportError` → `CapabilityError` exit 6 naming the pip package; `spec_judge` wrapper in `scorers/judges.py`; generated native-judges capability section; no `plan`/`evaluate`/`calibrate` wiring (3b.3) |
| 3b.3 LiteLLM rubric fallback + wiring | done | `f8668fe` — `judges.<name>.judge` executor key (closed set, both layers); `scorers/litellm_judge.py` (full spec → `litellm.completion`, JSON mode, strict parsing: NaN/unrepresentable/non-number = judge-named exit 5, scale clamp, rubric-checked evidence, `report_usage` per call); core evaluator's usage collector now covers scoring (additive merge); `plan`/`evaluate` count client-side judge calls; collisions exit 2; the `litellm` extra |
| 3b.4 pairwise + panel wrappers | done | `59518c2` — `scorers/panel.py` (`normalize_pairwise` closed verdict set; both-orders pairwise + `swap_agreement`; mean/majority/agreement panel + always-on `panel_disagreement`; construction exit-2 validation), app-hook composables, no config surface, vendor-free |

**G0 closed.** CI run `40047473262` on `main`: py3.10 / 3.11 / 3.12 all green.
**G1 closed** (2026-10-24, at 283 tests): the full checklist and evidence are in
[`HANDOFF.md`](../HANDOFF.md) §6; CI `40078618397`.

**G2 closed** (2026-10-24, tagged `v0.4.0` at 359 tests): pipeline parity (the four stage tables
match the bash stages and `--dry-run` lists them), seven presets (`init --preset` ×7, zero-drift
capability matrix), calibration enforced (a below-threshold calibration fails real `pr`/`main` and
the stage error names the failing check). Evidence: [`HANDOFF.md`](../HANDOFF.md) §6.

**Next:** phase **3b** continues (judges, native-first; D6) — WP3b.1 (`173a416`), WP3b.2 (`c2beda5`),
WP3b.3 (`f8668fe`) and WP3b.4 (`59518c2`) are done: a declared `JudgeSpec` resolves to a real
scorer (native or LiteLLM), and the comparison wrappers (pairwise position-swap with reported
`swap_agreement`; panel with always-reported `panel_disagreement`) compose any of them. The last
3b WP is **WP3b.5**: pin enforcement for **every** judge (gate refusal exit 1; `judge_meta()`
never overrides the config pin; `unpinned` becomes exhaustive), resolved model + prompt version
recorded per verdict, the nightly server-side settings check, and calibration as a gate input.
**G3b** tags `v0.5.0`. Phase 3 (vendor contract verification) comes after 3b and needs
docker + credentials. Live status: [`docs/STATE.md`](STATE.md) · entry point + coordination rules:
[`HANDOFF.md`](../HANDOFF.md) §0/§4.

**Next increment:** phase 3b — write `docs/packets/WP3b.5.md` (from the phase-3b row above and
`docs/packets/WP3b.4.md`, the newest template) and dispatch it to `evalops-worker` after
`HANDOFF.md` §4's coordination fetch check. WP3b.5 touches `gate.py` and the verdict-recording
path: expect the packet to need CONTRACT §6's enforcement bullets turned into done-when items.

### Phase 0 delivered

- typed config (`src/evalops/config/`, generated schema, `evalops config`)
- frozen JSON envelope + exit codes 0–7 pinned by 17 goldens and an exit matrix
- versioned persisted formats with semantic-only identity hashes
- contract doc, 7 ADRs, `make check`, CI workflow, vendor-import guard
- 17 → 125 tests, all offline

## 7. Review log

| Round | Reviewer verdict | Defects raised | Closed in |
|---|---|---|---|
| G0-1 | BLOCK | 7 (format bypasses, scanner opt-out, config not strict, unversioned artifact, weak R1 proof, doc overreach, out-of-scope files) | WP0.4b |
| G0-2 | BLOCK | 1 (scanner alias/chained re-raise + raising sibling handlers) | WP0.4c |
| G0-3 | BLOCK | 1 (handler calling an external helper) | WP0.4d |
| G0-4 | BLOCK | 2 (docstring over-claimed "nothing that can raise"; untracked file) | WP0.4e |
| G0-5 | BLOCK | 2 (AugAssign doc/code drift; expression-statement whitelist mismatch) | bf553d4 |
| G0-6 | BLOCK | 2 (dict unpack `{**m}` accepted; F821/mypy delegation over-claimed) | 0349263 |
| G0-7 | BLOCK | 2 (unhashable literals not stated as a limit; stale `x += 1` example) | closed by orchestrator: doc corrected + boundary probes added (0349263+) |
| WP0.3-1 | BLOCK | 3 (`max_mae` rejected, plugin roles unconstrained, out-of-scope docs) | WP0.3b |
| WP0.3-2 | BLOCK | 2 (schema allowed null role values the runtime rejected; `max_mae: null` crashed calibration) | WP0.3c + orchestrator `threshold: null` fix |
| WP0.3-3 | PASS | 0 (residual: `threshold: null`, fixed by orchestrator immediately) | f0da4ce |
| WP0.5-1 | BLOCK | 7 (argparse/YAML escaped the envelope, stdout pollution, 7 goldens only, env masked, weakened assertion, stale docstring) | WP0.5b |
| WP0.5-2 | BLOCK | 3 (sys.__stdout__/subprocess capture, early envelopes ignored default_env, contract overclaimed coverage) | WP0.5c |
| WP0.5-3 | BLOCK | 3 (in-process capsys capture, recursive-alias env crash, option value parsed as subcommand) | WP0.5d |
| WP0.5-4 | PASS | 0 | 7d7d8f1 |

Lesson carried into later phases: a guard that reasons about "does this code raise?" must be
syntactic and conservative, and every guard must state — and pin with a test — exactly what it
cannot see. Diminishing returns are real: after round 4 the remaining defects were documentation
accuracy, not behaviour. Future review gates cap doc-only iterations at two rounds and then close
by orchestrator decision.

Phase-1 and phase-2 review rounds (1–4 rounds per WP; every BLOCK round found something real) are
logged per WP in [`HANDOFF.md`](../HANDOFF.md) §4's observed-budget table rather than duplicated
here.

### Orchestrator closures

- **G0** (phase 0) — accepted. 52→55 tests, `make check` green on the local interpreter; the
  Python 3.10–3.12 matrix is configured but has not run on GitHub yet (R11 target for WP0.6).
  Known compromise tracked for WP1.1: `ruff format --check` covers `tests/` only; `src/` is
  `ruff check`-clean but not format-enforced until the tree is quiet.

