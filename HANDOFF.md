# HANDOFF: evalops — phase 0 done, phase 1 next

Repo: **https://github.com/minghao51/evalops** (public) · branch `main` · CI green on 3.10/3.11/3.12
Read this file, then `docs/IMPLEMENTATION_MAP.md` (the plan) and `docs/CONTRACT.md` (the invariants).
`docs/STATE.md` is the short status board.

---

## 1. What this is

Backend-agnostic versioning, evaluation, gating and cost control for LLM apps **and agents**.
evalops delegates execution, tracing, storage and model calls to a backend and owns only the glue:
version pinning (prompts/datasets/behavior config/judges), the 5-role plugin contract, normalized
agent traces, statistics + gates, the CLI/pipeline contract, safety (redaction, budgets, breaker) and
reproducibility (cassettes).

- **Backends:** MLflow / Databricks, Langfuse, LangSmith, Logfire, plain git + SQL, or your plugin.
- **Interface:** one CLI for humans, CI and coding agents — stable exit codes `0–7`, frozen JSON envelope.
- **History:** v0.3.0 imported as the baseline (`0a538e2`, 17 tests). Phase 0 added the contract,
  typed config, versioned formats, envelope and dev tooling. **Now 125 tests, all offline.**

## 2. Non-negotiables (read before editing anything)

1. **Contract first.** `docs/CONTRACT.md` is normative: exit codes, envelope shape, the five port
   invariants, identity-hash rules, format policy, judge pinning. Changing it needs an ADR + a map entry.
2. **Formats are additive and versioned.** `FORMAT_VERSION = 1`. Readers accept v0; a future
   `schema_version` is exit 7 naming the file, never a silent misread. Identity hashes (cassette,
   dataset fingerprint, `app_version`) cover **semantic fields only** — stamping metadata must never
   change them (pinned by the constant `73357e6c4b54`).
3. **Core is vendor-free.** No module-level import of mlflow/langfuse/langsmith/logfire/litellm/
   langchain/openai/pydantic_ai/otel/sqlalchemy outside `src/evalops/plugins/` and
   `src/evalops/traces/`. Enforced by an AST guard in `tests/unit/test_no_vendor_imports.py`; read
   its docstring for what it deliberately cannot see.
4. **Nothing outside the envelope on stdout** in `--json` mode; diagnostics go to stderr. Exit code
   must equal `envelope.code`.
5. **Never weaken a gate, test or guard to go green.** If a test's expectation is genuinely obsolete,
   replace it and say why in the commit body and the worker report.
6. **A green suite is not evidence a requirement is met.** Reviewers falsify the done-when.

## 3. Working environment (important)

There is **no system pip** on this machine (Ubuntu, externally managed). Use the existing `.venv`
or `uv`:

```bash
cd evalops
.venv/bin/python -m pytest -q          # 125 tests
.venv/bin/python tests/run_without_pytest.py   # dependency-free runner
make check                             # ruff + mypy + pytest (what CI runs)
make schema                            # regenerate docs/evalops.schema.json
```

Fresh clone: `uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"`.

## 4. How work is dispatched here (proven protocol)

Three agents, defined in `.pi/agents/` (project scope, symlinked at the session root `.pi`):

| Agent | Model | Role |
|---|---|---|
| `evalops-scout` | `deepseek/deepseek-flash` | read-only recon, compressed findings with `file:line` |
| `evalops-worker` | `deepseek/deepseek-flash` | implements exactly one WP, tests green, **no commits** |
| `evalops-reviewer` | `openai-codex/gpt-6-luna` @ xhigh | adversarial review: runs commands, falsifies claims, PASS/BLOCK |

Per WP: **packet → worker → reviewer → orchestrator commits**. The packet must state goal, allowed
paths, forbidden actions, falsifiable done-when, and the exact commands to run. Writers are
serialized per module family; at most two parallel workers in disjoint trees.

Observed in phase 0: 12 BLOCK rounds / 35 defects. Phases 1–2 WPs will take 2–3 rounds each.
Budget for it; do not skip review to "save time" — every round found something real, including a
functional regression (`rollback --version 1` being parsed as the top-level version flag).

Commit style: one logical change per commit, imperative subject, body explains the *why*, the WP id,
and the review rounds. Never commit with a red suite.

## 5. Where phase 0 left off

| WP | State | Evidence |
|---|---|---|
| 0.0 repo/agents/map | done | `16ce2e1` |
| 0.1 contract + ADRs 0001–0007 | done | `6280873` |
| 0.2 test layout, ruff/mypy, `make check`, CI, vendor-import guard | done | `6280873`…`09c26fb` |
| 0.3 typed config (pydantic v2) + generated schema + `evalops config` | done | `f0da4ce` |
| 0.4 versioned formats + semantic fingerprints | done | `6280873` |
| 0.5 envelope freeze + 17 goldens + exit matrix 0–7 | done | `7d7d8f1` |
| 0.6 state/changelog/**first CI run** | done | run `40047473262`: 3.10/3.11/3.12 all green |

Two compromises carried forward:
- `ruff format --check` covers `tests/` only; `src/` is `ruff check`-clean. Fix in WP1.1 by formatting
  `src/` and widening the Makefile target.
- Vendor plugins are still **unverified** (`VERIFY` markers) and logfire reads a private SQL table.
  That is phase 3, behind docker/credentials.

## 6. Phase 1 — do this next

Full detail in `docs/IMPLEMENTATION_MAP.md` §2. Start with **WP1.1** (highest user risk: data
currently persists unredacted), then 1.3, 1.2, 1.4, 1.5. They are independent enough to run 1.1 and
1.3 in parallel (disjoint trees).

### WP1.1 — redaction (do first)

Currently traces, `rows.jsonl`, cassettes, ledger artifacts and `last_eval.json` store payloads
verbatim (only length-clipped). A redaction hook is the gate for any real data.

Done-when:
- `evalops.contract.redact` with ordered rules: field-path globs, regex patterns, `hash` mode, and a
  callable hook; applied at exactly three outbound boundaries (ledger artifacts, `AgentTrace.to_dict`,
  cassette save), **after** identity hashing.
- `evalops redact --check` renders the diff it *would* apply without writing.
- `EVALOPS_REDACT=strict|off`; strict refuses to persist unredacted payloads that match a rule.
- Property test: a planted secret never reaches any sink; redaction is idempotent; `app_version` and
  both fingerprints are unchanged by redaction.
- Wire config: typed `redact:` section (pydantic model + regenerated schema).
- Widen `ruff format --check` to `src/` in the same WP.

### WP1.3 — statistics (can run in parallel with 1.1)

- One stdev convention (sample), Wilson + bootstrap CI, `pass^k` documented exactly, **stable row
  identity** (dataset fingerprint + row key, not the list index — reordering the dataset currently
  misaligns repeats), baseline pinned by group+hash, real drift math, change attribution in the gate
  report.
- Property tests: `pass^k` monotone in k, CI shrinks with n, reordered dataset keeps alignment.

### WP1.2 / 1.4 / 1.5

- 1.2 ledger integrity: Postgres via docker, `evalops ledger migrate|verify`, atomic budget
  reserve/settle, UTC month boundary, N+1 fix.
- 1.4 test debt: unit tests for `gate.reliability/ci_lower/check_rows`, cassettes, `calibrate._stats`,
  `evalops traces`, `promote --candidate/staging`, `rollback`, plan budget failure.
- 1.5 trace model v2 + unknown-attribute reporting.

## 7. Open decisions / blockers

| Item | State | Needs |
|---|---|---|
| **Distribution name** | keep import `evalops`; PyPI `evalops`/`evalops-sdk` and GitHub org `evalops` belong to Deixic, Inc. (observed 2026-10-23). `evalplane`, `agentgates`, `evalops-core` are free. | decide in WP6.1; ADR 0001 |
| **LICENSE file** | ADR 0002 chose Apache-2.0; no file/metadata yet (deliberately deferred to WP6.1) | add at WP6.1 |
| **Phase 3 credentials** | docker is available; LangSmith/Logfire/Databricks need keys | ask the user before phase 3 |
| **`config explain` shape** | `data.data` / `data.provenance` (kept for payload stability) | rename only with a contract bump |
| **Cost goldens** | amounts are masked (types pinned) | tighten if cost accounting regresses |

## 8. Map of the code

```text
src/evalops/
  contract/formats.py    FORMAT_VERSION, _meta, semantic hashes, FormatError(exit 7), manifest
  config/                pydantic models, generated schema, load/Config/provenance, `evalops config`
  envelope.py            envelope builders, EXIT_MATRIX, one-document render
  cli.py                 argparse + dispatch + envelope + fd/stream capture + --version
  ports.py               5 role ABCs + dataclasses (RowResult/Usage/LoadedPrompt/RunRecord)
  registry.py            builtin + entry-point + "module:Class" plugins, role capability check
  run.py                 plan/evaluate/baseline/show_traces (the only way evaluations run)
  gate.py                aggregate + check + pass^k/pass@k + CI lower bound + report
  cost.py                estimate/measure/budget/month spend
  calibrate.py           judge calibration vs human labels (agreement/kappa/MAE)
  guard.py               static bypass guard
  scoring.py             neutral scorer protocol + normalization + aggregation
  tools.py               cassettes (record/replay), semantic fingerprint
  locks.py               per-env locks, reserved _meta, entries()
  prompts.py datasets.py prompt/dataset sync, content-addressed snapshots, aliases
  traces/                model (AgentTrace), normalize (4 dialects), capture, instrument, langchain, openai_agents
  scorers/               trajectory scorers + judge adapters
  plugins/               git, sql, core, mlflow, langfuse, langsmith, logfire
tests/
  unit/                  contract/formats, config, envelope, guard, scorers, traces
  integration/           full lifecycle on zero-dep plugins (local + agent)
  fixtures/              fake apps, v0 format fixtures, envelope goldens
docs/                    IMPLEMENTATION_MAP, CONTRACT, STATE, PLUGINS, AGENT_TRACES, MLFLOW_*, adr/
ci/                      pipeline.sh + GH/GitLab/Azure adapters (phase 2 replaces with `evalops pipeline`)
```

## 9. First three commands

```bash
cd evalops && make check
gh run list --limit 1                 # confirm CI is green on main
sed -n '/## 2. Work packages/,/## 3. Testing/p' docs/IMPLEMENTATION_MAP.md
```

Then write the WP1.1 packet and dispatch `evalops-worker`; send the diff to `evalops-reviewer` at
xhigh before committing.
