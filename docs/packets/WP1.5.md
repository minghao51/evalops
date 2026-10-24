# WP1.5 packet — trace model v2, unknown attributes, breaker parity

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **246 tests** (+1 contract, deselected).
Read `docs/CONTRACT.md` §4 (identity), §5 (format policy) and `docs/AGENT_TRACES.md` before editing.

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 246 tests
.venv/bin/python tests/run_without_pytest.py
make check
```

No network, no docker, no vendor SDK installs. Reuse the WP1.4 harness `tests/unit/_projects.py`
(`project`, `ledger_project`, `add_run`, `run_cli`, `fixture_text`) instead of a new one.

## Goal

Make the normalized agent trace a **versioned, self-describing document** without changing any
identity hash or gate outcome, report unmodeled span attributes instead of silently dropping them,
and pin the circuit-breaker behaviour of every capture path with a regression test.

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. Trace schema version (`traces/model.py`, `traces/__init__.py`, `run.py`)

- **A1** `TRACE_SCHEMA_VERSION = 2` in `traces/model.py`, exported from `traces/__init__.py`.
  `AgentTrace` gains `schema_version: int = TRACE_SCHEMA_VERSION` (last field, keyword-safe).
- **A2** `AgentTrace.to_dict()` writes a top-level `"schema_version": 2` next to `trace_id`,
  `source`, `spans`. Writers always emit 2 (`to_dict` ignores a read `schema_version=1`; that is the
  upgrade path). Redaction still runs last and must not alter the version.
- **A3** `AgentTrace.from_dict(d, source=None)`:
  - `None`/`{}` → `None` (unchanged);
  - absent `schema_version` means **v1**; explicit `1` and `2` are accepted; the returned object
    records the version it read (`schema_version` attribute);
  - anything else (3+, `0`, a string, `null`, a bool) raises `FormatError(source, found, 2)` →
    **exit 7**, naming the source. Default source when none is passed: `"trace"`.
  - unknown **top-level** keys are ignored; unknown **span** keys are ignored (filter through
    `dataclasses.fields(Span)`), never a `TypeError`.
- **A4** `run.rows_from_artifact` passes its `source` into `from_dict`, so a future trace version
  names `evalops/rows.jsonl` on the real `evalops traces` path. The calibration path in
  `run.evaluate` passes `source=f"dataset:{dataset}"`.
- **A5** `SOURCE` value set is documented as exactly
  `("otel", "langchain", "openai_agents", "mixed", "stored", "unknown")`; every value round-trips;
  `from_dict` with no `source` yields `"stored"`.
- **A6** **No identity-hash change.** The trace version is metadata:
  - the v1 and v2 fixtures (section D) share inputs/expectations and therefore the same `row_key`,
    pinned to `datasets.row_key(...)`;
  - `datasets.fingerprint` over the fixture's dataset source records is a pinned constant and the WP
    must not move it;
  - `app_version`/`cfg_hash` inputs are unchanged (the trace document is not one of them);
  - a v1 rows artifact and the same rows upgraded to v2 produce identical `trace_metrics` and
    identical gate `checks` (section D2).

### B. Unknown span attributes (`traces/normalize.py`, `traces/model.py`, `run.py`, `cli.py`)

- **B1** `Span` gains `unknown_attributes: list[str] = field(default_factory=list)` (last field).
  It holds **attribute names only, sorted, deduplicated** — never values (a value may be a secret).
- **B2** `normalize()` declares the attributes it actually consumes (`KNOWN_ATTRS`, covering every
  alias it reads: operation/kind markers, model, token counts, tool args/result, agent names,
  generic input/output, `metadata`, `error.type`, … plus `KEEP_ATTRS`). Anything else in the raw
  span is reported in `span.unknown_attributes`. A consumed attribute (e.g.
  `gen_ai.usage.input_tokens`, `input.value`, `openinference.span.kind`) is **not** unknown.
- **B3** `AgentTrace.unknown_attributes()` returns `{name: span_count}` (sorted, deterministic) over
  all spans. `Span.attributes` (the allowlisted values) keeps its current meaning.
- **B4** `to_dict()` carries per-span `unknown_attributes` (additive); a rows-artifact
  `rows_from_artifact` round-trip retains them; `trace_metrics()` keys and values are unchanged.
- **B5** New flag `evalops traces --unknown-attrs` (`store_true`; `cli.py` parser + dispatch +
  `run.show_traces(..., unknown_attrs=False)`):
  - **default output is byte-identical** to today (the frozen `traces` golden must not move);
  - with the flag: each row gains `"unknown_attributes": [<sorted names>]` and `data` gains
    `"unknown_attributes": {<name>: <span count>}` (additive `data` keys only);
  - `--row` filtering still applies.
- **B6** A planted value under an unmodeled attribute must never appear in the envelope; only its
  name may appear.

### C. Breaker parity (`traces/capture.py`, `traces/openai_agents.py`, `plugins/core.py` only if a
genuine bug is proven — report first)

- **C1** `capture.BREAKER_PARITY` documents the four capture paths, and `docs/AGENT_TRACES.md` has
  the matching table plus the framework hard stop to set alongside:
  - `langchain: "enforced"` — `on_llm_start`/`on_tool_start` raise `BudgetExceeded` before the step;
  - `otel: "post-hoc"` — spans are observed; `cap.exceeded` becomes a row error after predict;
  - `openai_agents: "post-hoc"` — the SDK swallows processor exceptions; set `max_turns`; async runs
    are cut off by `row_limits.max_seconds`;
  - `custom: "cooperative"` — `check_budget()` raises.
- **C2** Do **not** add a `check()` call that pretends the OTel/OpenAI paths can abort. Keep the
  honest post-hoc path.
- **C3** Regression test per path (one named test each; the OTel and OpenAI tests may share the
  post-hoc assertion, but the capture entry point must be the real one):
  - LangChain raises `BudgetExceeded` before the next LLM/tool call;
  - OTel: synthetic `CaptureProcessor` spans exceed `max_steps` → no raise, `cap.exceeded` set, and
    a real `evalops eval` row over that capture is `budget_exceeded` (`budget_exceeded_rate > 0`);
  - OpenAI Agents: synthetic processor spans exceed `max_steps` → no exception escapes, `cap.exceeded`
    set (the SDK-swallow note is documentation, not a test claim);
  - custom: `check_budget()` raises inside a row and is a no-op outside one;
  - `max_seconds`: an **async** predict slower than the limit is cut off (assert the actual row
    error; do not relabel a `TimeoutError` as `budget_exceeded`). State in the docs that sync
    predict cannot be interrupted.

### D. Fixtures + compatibility

- **D1** Add `tests/fixtures/traces/rows_v1.jsonl` and `rows_v2.jsonl`:
  - same row semantics (inputs/expectations/outputs/scores/usage/trace metrics), one row is enough;
  - v1: rows line `schema_version: 1`; nested `trace` has **no** `schema_version` (legacy v1);
  - v2: the same trace document **with** `"schema_version": 2`, every span carrying
    `"unknown_attributes"` (`[]` except one planted name, e.g. `"weird.vendor.attr"`).
  - Generating the v2 document with `AgentTrace.from_dict(v1_trace).to_dict()` is the expected way
    to keep the pair exact; pin the equality in a test so the fixtures cannot drift.
- **D2** A v1 artifact still gates: a temp project with the in-memory ledger (`_projects.ledger_project`
  + `add_run` with `artifacts={"evalops/rows.jsonl": <fixture text>}`), a `gates.reliability.success`
  rule and/or `min_ci_lower` row rule + `gate --group` → exit 0, and the v1 and v2 runs produce the
  **same checks list** and same `passed`.
- **D3** `evalops traces --group` reads both fixtures; `--unknown-attrs` on v2 names the planted
  attribute and contains no planted value; on v1 it reports `{}` / empty lists.
- **D4** `AgentTrace.from_dict(v1_trace).to_dict()` equals `rows_v2`'s trace document; both traces
  have equal `metrics()`; the v0 rows fixture (`tests/fixtures/formats/v0/rows.jsonl`, `trace: null`)
  still reads.

### E. Documentation (allowed and required)

- **E1** `docs/CONTRACT.md`: add the additive §5 row for the **nested trace document**
  (`evalops/rows.jsonl` `trace` field): `TRACE_SCHEMA_VERSION = 2`, absent = v1, readers accept
  1–2, future = exit 7 naming the artifact, and the trace version never enters `app_version`,
  dataset fingerprints or `row_key`. This is an additive §5.6 change, not an invariant change; no
  ADR. If you conclude it needs an ADR, stop and report.
- **E2** `docs/AGENT_TRACES.md`: document the trace document version, the `SOURCE` set, the
  `--unknown-attrs` flag and its shape, the breaker-parity table (including the async-only
  `max_seconds` caveat), and that attribute **names** are reported, never values.
- **E3** No config change: `docs/evalops.schema.json` must not change (`make schema` zero drift).
  No envelope golden may change; the default `traces` payload is frozen.

## Non-goals

- No real framework/SDK installation or new fixture per vendor (that is phase 5).
- No v3, no new config keys, no gate threshold or `trace_metrics` change.
- No change to redaction rules, `<FORMAT_VERSION>` (stays 1), rows line version, or the envelope.

## Allowed paths

`src/evalops/traces/**`, `src/evalops/run.py`, `src/evalops/cli.py`, `docs/CONTRACT.md`,
`docs/AGENT_TRACES.md`, `tests/unit/**` (new files + additive edits), `tests/fixtures/traces/**`
(new), and `tests/fixtures/**` only if genuinely needed (say why). `src/evalops/plugins/core.py`
only for a genuine bug found by a test, reported in `Blockers` with the failing test and a minimal
fix. Mutation experiments may temporarily touch other `src` files but **must be reverted**.

## Forbidden

`docs/IMPLEMENTATION_MAP.md`, `docs/STATE.md`, `CHANGELOG.md`, `HANDOFF.md`, `README.md`, `.pi/**`,
`.github/**`, `pyproject.toml`, `docs/evalops.schema.json`, `tests/fixtures/envelope/**`, changing
gate thresholds/semantics or `trace_metrics`, deleting/skipping/loosening tests, new dependencies,
git commit/push/branch/amend.

## Required coverage map (name each test in the report)

| Item | What must be proven |
|---|---|
| A1/A2 | constant + `to_dict` writes 2 |
| A3 | absent=1, explicit 1/2 accepted, future/non-int exit 7 naming the source; unknown span key ignored |
| A4 | `evalops traces` on a rows artifact with `trace.schema_version: 3` → exit 7 naming `evalops/rows.jsonl` |
| A5 | v1→v2 upgrade; v2 round-trip; all `SOURCE` values; default `stored` |
| A6 | pinned `row_key`, pinned dataset fingerprint, equal `trace_metrics`, equal gate checks |
| B1–B4 | names only (sorted/deduped), consumed attrs excluded, aggregate method, rows round-trip, metrics unchanged |
| B5 | flag surfaces per-row + aggregate; default output/golden unchanged; `--row` works |
| B6 | planted value under an unknown key never emitted |
| C1–C3 | four `BREAKER_PARITY` entries and one real regression test per path + async `max_seconds` |
| D1–D4 | fixtures exact, v1 gates, v1/v2 same checks, v0 still reads |
| E1–E3 | contract/AGENT_TRACES accurate; schema/goldens untouched |

## Mutation evidence (required)

At least **10** mutations, at least **2 per section A–E**, each with the named test that goes red and
the quoted failing assertion, then reverted. Suggested: drop `schema_version` from `to_dict`; make
`from_dict` accept any version; require `schema_version` (v1 unreadable); make `unknown_attributes`
include a consumed key; store unknown **values** instead of names; invert the flag default; make
`show_traces` ignore the flag; add a fake `check()` to the OTel path; remove the OpenAI
`started_steps` increment; make `check_budget` a no-op; break the v1 fixture equality. End with a
`git diff --stat` proving only the intended `src`/`docs`/`tests` changes remain.

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format src tests
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/unit/test_traces.py tests/unit/test_trace_format.py tests/unit/test_trace_breaker.py -v   # names may differ
.venv/bin/python tests/run_without_pytest.py
make check
make contract
make schema && git diff --stat docs/evalops.schema.json
git status --short
```

## Falsifiable done-when

- **H1** Every A–E item maps to a named test; the report's table has no gaps or narrowed items
  (e.g. "tested the helper, not `evalops traces`" is a block).
- **H2** A future **nested trace** `schema_version` reaches exit 7 through a real `evalops traces`
  command and the message names `evalops/rows.jsonl`; the existing rows-line exit-7 test still passes.
- **H3** The default `traces` output is unchanged (`tests/fixtures/envelope/traces.json` diff is
  empty) and the flag is the only way new fields appear.
- **H4** No identity hash moves: pinned `row_key` and dataset fingerprint; v1 and v2 fixtures gate
  identically.
- **H5** Every breaker claim in `AGENT_TRACES.md` is backed by a test and matches the code; no
  pretend enforcement.
- **H6** `make check`, standalone runner, `make contract` skip, `make schema` zero drift, goldens
  untouched, no new dependency.
- **H7** Mutation evidence ≥10 rows (≥2 per section), all red, all reverted; final `git status`
  shows only the intended files.
- **H8** Docs (CONTRACT §5, AGENT_TRACES) describe the shipped code, not the plan.

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Coverage map
<A–E item → tests/unit/<file>::<test>>

## Mutation evidence
| Branch (file:line) | Mutation | Test that went red | Assertion |
|---|---|---|---|

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the trace-version matrix, the flag shape, breaker table, git status>

## Deviations
<none | list with reason, incl. any src fix and the bug it proved>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `evalops-reviewer`, model `zai/glm-5.3-flash`, thinking high)

WP1.5 (trace model v2) review, uncommitted tree at `/home/howt/work/evalops_universal/evalops`.
Read `docs/packets/WP1.5.md`, then `docs/CONTRACT.md` §4–§5 and `docs/AGENT_TRACES.md`.
Treat the worker's coverage map and mutation table as claims.

- **H9** **Falsify the version story.** v1 (absent) reads, v2 writes, future/non-int is exit 7
  through a real `evalops traces` naming the artifact. Check that `to_dict` really upgrades a read
  v1, that unknown span keys are ignored, and that the v1/v2 fixtures are the same trace (not two
  different ones). Re-run the version mutations on a `/tmp` copy.
- **H10** **Hunt vacuous unknown-attribute tests.** A consumed attribute must not be reported; a
  vendor attribute must be, by name only; the default `traces` payload must be byte-identical
  (golden diff empty). Ensure the flag test fails if the flag is ignored or defaulted on.
- **H11** **Breaker honesty.** Read the four paths. No fake enforcement in `openai_agents.py` or
  the OTel path; the post-hoc conversion must be tested through a real eval row; the `max_seconds`
  claim must match async-only behaviour. Invent a mutation of your own (e.g. silence the post-hoc
  `cap.exceeded` branch) and confirm a named test goes red.
- **H12** **Identity and compatibility.** Re-derive the pinned `row_key` and dataset fingerprint;
  confirm the trace stamp is not an input to `app_version`/fingerprints. Diff the default `traces`
  golden; confirm v0 rows still read and v1 artifact gates.
- **H13** **Scope.** `git diff` must stay within the packet's allowed paths; no gate/metric/config
  change, no deleted/skipped/loosened test, no new dependency; `make schema` zero drift; `make
  contract` still skips.
- **H14** **Report honesty.** Every A–E item has a real test; flag any narrowed claim. Re-run the
  worker's ≥10 mutations plus 3 of your own on `/tmp`; any test that stays green is a defect.

Verdict format exactly as the agent file defines (`## Verdict`, `## Evidence`, `## Defects`,
`## Residual risk`). BLOCK on any vacuous claim, missing A–E item, identity/golden movement, or
scope violation.
