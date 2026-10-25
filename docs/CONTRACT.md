# evalops public contract (v1)

Status: **frozen by WP0.1** (2026-10-23). This document is normative for the CLI, the plugin port
layer, persisted artifacts, and third-party plugins. Changing an invariant here requires an
orchestrator decision in [`docs/IMPLEMENTATION_MAP.md`](IMPLEMENTATION_MAP.md) plus an ADR in
[`docs/adr/`](adr/).

Version identifiers:

| Identifier | Value | Meaning |
|---|---|---|
| `CONTRACT_VERSION` | `1` | This document |
| `ENVELOPE_SCHEMA_VERSION` | `1` | `--json` output shape (frozen, WP0.5) |
| `FORMAT_VERSION` | `1` | Persisted artifacts (implemented in WP0.4) |

Where this document describes a target that a later WP implements, the WP number is named. The
frozen rules (exit codes, port invariants, identity-hash rules, format policy, judge pinning) are
binding now, including on code written before the implementation WP lands.

---

## 1. Exit codes

The process exit code is the primary machine interface. Agents and CI branch on it and never parse
human output. Codes are **never renumbered**.

| Code | Name | Meaning | Typical cause |
|---|---|---|---|
| 0 | `OK` | Command succeeded and every applicable gate passed | — |
| 1 | `GATE_FAILED` | A gate, reliability check, calibration, or baseline comparison failed | quality drop, `pass^k` below threshold, kappa below floor |
| 2 | `USAGE` | Usage, configuration, or schema error | unknown key, bad flag, unsynced dataset, unpinned judge at load |
| 3 | `BUDGET` | Estimated or actual spend exceeds the configured budget | `cost.budgets.per_run_usd`, monthly cap |
| 4 | `GUARD` | A static or safety guard rejected the operation | dirty tree, frozen dataset edited, direct vendor SDK call, lock drift |
| 5 | `RUNTIME` | Unexpected failure | backend I/O error, corrupt artifact, unhandled exception |
| 6 | `CAPABILITY` | The configured plugin does not support the requested operation | `git` used as a ledger, no alias support |
| 7 | `CONTRACT` | A persisted artifact or envelope declares a format this evalops does not understand | future `schema_version`, non-object artifact |

Rules:

1. A successful command exits `0` **iff** its JSON envelope has `ok: true`.
2. Every failure path returns one of `1`–`7`; a command may never convert a failure into `0`.
3. `6` is always accompanied by an actionable hint naming the plugin, the operation, and the
   supported alternatives. Callers must surface the hint and must not work around it.
4. `7` is raised when reading, never when writing: writers always emit `FORMAT_VERSION`.
5. New codes may only be added by a new ADR; existing meanings are append-only.
6. `argparse` usage errors are normalized to `2`; `--help` and `--version` are `0`.
7. Ledger integrity commands (WP1.2) reuse this matrix without adding a code: `evalops ledger migrate`
   is `0`; a clean `evalops ledger verify` is `0`, while a report with integrity problems is `5`
   (its `errors[0].message` names the problem count and `data.problems` carries the typed findings);
   a ledger plugin without schema support is `6` (`CapabilityError`); a recorded ledger table schema
   newer than this evalops understands is `7` (`FormatError`, on both `migrate` and `verify`).

The mapping is asserted, one case per code `0`–`7`, by
`tests/unit/test_envelope.py::test_exit_code_matrix_zero_to_seven`, which checks the process exit
code and `envelope["code"]` together. That matrix is a code-path sample, not a per-command claim:
coverage of individual commands is stated in §2.

### 1.1 Configuration (`evalops.yaml`)

`evalops.yaml` is typed: [`src/evalops/config/models.py`](../src/evalops/config/models.py) holds the
pydantic v2 model set (D4) and [`docs/evalops.schema.json`](evalops.schema.json) is generated from
those models (`make schema`, drift-tested in `tests/unit/test_config_models.py`).

- Validation runs on the **merged** document (top level ⊕ the selected `environments.<env>`
  block), because env blocks are partial overlays by design. Every other block is validated too
  (merged over the top level, all sections optional), so a typo in an unused block still fails.
- An unknown key or a wrong type is **exit 2**, and the message names the full dotted key path
  (`gates.metrics.facts/mean.minn`, `eval.retries`). The error details carry
  `errors: [{"path", "message"}]` alongside `env` and `path`.
- Two key sets are **closed** because the runtime reads their names literally, so a typo is exit 2
  with a dotted path instead of a silently ignored setting:
  - `plugins.<role>` — exactly the five roles `prompts | datasets | ledger | tracer | evaluator`
    (`ports.ROLES`), at the top level (`plugins.promps`) and in every `environments.<env>` block
    (`environments.<env>.plugins.promps`, selected or not).
  - `eval.row_limits` — exactly `max_steps`, `max_tokens`, `max_seconds` (the keys
    `traces/capture.py` enforces and `plugins/core.py` uses as the async timeout); `max_step` is
    `eval.row_limits.max_step`. **Adding a new limit requires updating the model in
    `src/evalops/config/models.py` in the same change as the runtime that consumes it** — the
    schema, the dotted-path error and the documentation all follow from the model.
- `judges.<name>` accepts `min_agreement`, `min_kappa`, `max_mae`, `label`, `threshold`, `model`
  (`max_mae` is the `mae <= max_mae` threshold `evalops calibrate` enforces for scalar judges).
- `${VAR}` / `${VAR:-default}` expansion happens before validation and only substitutes text: an
  env var can never inject a mapping or a list.
- The runtime view is unchanged from v0.x: `Config.data` is the plain merged dict, and
  `section/plugin_options/path/lock_dir/hook/plugins` behave as before.
- `evalops config validate|schema|explain` expose the models; `explain` adds per-key provenance
  (`top`, `env:<name>`, `default`). `--json` shapes are additive and do not form part of the v1
  envelope (WP0.5) until a golden snapshot pins them.

## 2. The `--json` envelope (v1)

Every command supports `--json`. With `--json`, stdout contains exactly **one** JSON document and
nothing else; diagnostics go to stderr. Without it, human output is free-form and not a contract.

```json
{
  "ok": false,
  "code": 3,
  "schema_version": 1,
  "command": "eval",
  "env": "local",
  "data": {"estimate": {"total_usd": 0.42}, "budget": {"ok": false}},
  "errors": [
    {"code": 3, "message": "estimated $0.42 exceeds per_run_usd $0.10",
     "hint": "raise the budget in an approved change or run `evalops plan` first",
     "details": {"estimated_usd": 0.42, "cap_usd": 0.1}}
  ]
}
```

| Field | Type | Rule |
|---|---|---|
| `ok` | boolean | Always present. `true` iff exit code is `0`. |
| `code` | integer | Always present. Equals the process exit code (`0` on success). |
| `schema_version` | integer | Always present. `1` for this envelope. |
| `command` | string | Always present. CLI subcommand (`plugins`, `guard`, `sync`, `plan`, `eval`, `gate`, …). |
| `env` | string | Always present. The **resolved** environment: `EVALOPS_ENV`, else `default_env` from the config, else `local`. Envelopes built before the config loads (`--version`, argparse errors) resolve it the same way (§2), and plan/sync/... report the env they actually ran in. |
| `data` | object | Always present, never `null`. Command-specific payload; `{}` when there is none. |
| `errors` | array | Always present. Empty on success. Each entry has `code` (int), `message` (string), optional `hint` (string) and `details` (object). |

Evolution rules:

- Top-level fields are frozen. New fields go inside `data` (or `errors[].details`) and are additive.
- Readers must ignore unknown fields and must not fail on a missing optional field.
- A success payload may contain command-specific `data` fields that are not listed here; those are
  contractual per command once a golden snapshot exists (WP0.5).
- v0 compatibility: evalops ≤ 0.3 emitted a legacy shape (payload spread at top level, `error`
  string, `code` only on failures, no `schema_version`/`command`/`data`/`errors`). Readers may
  accept the legacy shape until v1.0. Writers emit v1 only (WP0.5).
- Frozen (WP0.5): the deterministic commands each have a golden snapshot under
  `tests/fixtures/envelope/`, produced from one reproducible temp project built by the shared
  fixture in `tests/unit/test_envelope.py` (`src/evalops/envelope.py` holds `SCHEMA_VERSION` and the
  exit-code matrix). The snapshotted commands are exactly:
  `version`, `plugins`, `doctor`, `guard`, `baseline`, `cost`, `sync` (ok and `--check` failure),
  `plan`, `eval` (run ids, groups, timings and costs masked), `gate` (pass and fail), `traces`,
  `config validate` (ok and dotted-path failure), `config schema`, `config explain`.
  No other command has a golden. Stateful or credentialed commands cannot be reproduced offline from
  a temp project, so each one is covered by a test that does run:
  - `promote` and `rollback`: `tests/integration/test_e2e_local.py` promotes a main-stage evidence
    group to `production`, then `rollback --alias production --prompt … --version …` moves the alias
    back, and the following eval sees the uncommitted alias move (exit 4) before it is committed.
  - `calibrate`: `tests/integration/test_e2e_agent.py` (agreement on `judge_calibration`, then a
    broken judge is caught at exit 1) and `tests/unit/test_config_models.py` (`min_agreement`,
    `max_mae` and the `mae <= max_mae` threshold).
  - Evals against a vendor backend (MLflow, Langfuse, LangSmith, Logfire) are **not** covered yet.
    They are deferred to the phase-3 contract tests (`tests/contract/`, `@pytest.mark.contract`,
    excluded by default because they need docker-local services); nothing in this document claims
    they are tested until that suite exists.
- The goldens **pin** `env` to the environment the scenario selected instead of masking it, and the
  evalops `version` is not masked either, so running under the wrong `EVALOPS_ENV` or emitting a
  wrong version fails the snapshot. Masked: per-run identity (`group`, `run_id`, `trace_id`,
  `git_sha`, `app_version`, and the same tokens inside free-text summaries), absolute filesystem
  paths, and cost/timing numbers (prices and the clock are not contract). A relative file name such
  as `broken_config.yaml` and a dotted key path such as `gates.metrics.facts/mean.minn` stay
  visible: naming the offending file/key is part of the contract.
- A parse error (`argparse`: unknown flag, invalid choice, missing sub-command) is exit `2` **with**
  a code-2 envelope; the message is argparse's own text and names the offending argument, and
  `command` is the sub-command named on the command line (`cli` when none can be recognized).
  `--help` and `--version` stay exit `0` (`--json --version` is a normal version envelope). A
  sub-command's own `--version` option (`rollback --version 3`) is not the top-level flag: the flag
  only counts before the sub-command token.
- A malformed `evalops.yaml` (invalid YAML) is exit `2` with a code-2 envelope whose message names
  the file, never exit `5`: the config cannot be read, which is a usage/config error.
- In `--json` mode stdout carries the envelope and nothing else. Capture is at the **file-descriptor**
  level, not `sys.stdout`: a hook or plugin that writes to `sys.__stdout__` or spawns a child
  process is captured just like a plain `print`, and its text is forwarded to stderr
  (`test_fd_level_capture_stops_a_hook_bypassing_sys_stdout`).
- `env` is resolved, not defaulted: `EVALOPS_ENV`, else the project's `default_env`. The envelopes
  that are built before the config is loaded (`--version`, argparse errors) resolve it best-effort
  from the same file (`--config` / `EVALOPS_CONFIG` / `evalops.yaml` upward) and fall back to `local`
  when no config can be read; env resolution never turns those paths into a failure
  (`test_version_and_usage_envelopes_report_the_projects_default_env`).

## 3. Port contracts

Core code talks only to the five role interfaces in [`src/evalops/ports.py`](../src/evalops/ports.py):
`prompts`, `datasets`, `ledger`, `tracer`, `evaluator`. Port types are dataclasses, not pydantic
models, and a plugin may serve one or several roles.

### 3.0 Rules common to every plugin

- Declares `name` (stable, unique) and `provides` (the roles it actually implements). `provides` must
  match the ABCs the class subclasses; a role listed without the ABC is a configuration error (exit 2).
- A configured plugin that does not implement a requested role raises `CapabilityError` → **exit 6**
  with a hint listing its `provides` and the supported alternatives.
- Constructor and module import perform no network I/O and no backend writes. `doctor()` is
  read-only and returns `{"plugin": str, "ok": bool, ...}`; it never mutates backend state.
- A plugin writes only to its own backend/store. It never edits `.evalops/locks/**` or
  `.evalops/store/**` directly, and never mutates another role's records.
- Recoverable failures raise `EvalOpsError` with the correct exit code and a message naming the
  plugin, operation, and (when known) the backend response. Unexpected exceptions surface as `5`.
- Core code (outside `evalops/plugins/` and `evalops/traces/`) must not import a vendor SDK at module
  level (D4). Plugins import their SDK lazily, inside methods or behind `try/except ImportError`.

### 3.1 `PromptStore`

```python
register(name: str, template, tags: dict) -> str   # -> version id
load(name: str, version: str) -> LoadedPrompt
get_alias(name: str, alias: str) -> str | None
set_alias(name: str, alias: str, version: str) -> None
```

Invariants:

- **Register never overwrites.** Each successful `register` returns a version id; that id always
  resolves to exactly the registered template for the lifetime of the backend. Re-registering
  identical content may either return the existing version or create a new one, but a version id's
  content never changes. There is no update/delete operation.
- `load(name, version)` is exact. An unknown name/version is an error (exit 2 for a lock mismatch,
  5 for a backend failure), never a fallback to an alias or latest version.
- `get_alias` returns the pinned version or `None`; it never invents a default.
- **`set_alias` is a map update, not a pointer replacement.** Moving one alias must preserve every
  sibling alias byte-for-byte (`{"candidate": "1", "production": "2"}` → set `candidate` to `3`
  leaves `production` at `2`). Aliases never change the content behind a version.

### 3.2 `DatasetStore`

```python
exists(physical: str) -> bool
write(physical: str, records: list[dict]) -> None
read(physical: str) -> list[dict]
native(physical: str)                      # optional: backend object for native evaluators
```

Invariants:

- **Write-once per content hash.** Core derives the physical name as `<name>__<fingerprint[:8]>`.
  The same physical name is written at most once. A second `write` of the same name must either be a
  no-op or raise; it must never replace existing rows. Editing the source file changes the
  fingerprint, hence the physical name, hence creates a new snapshot.
- `read` returns `[{"inputs": {...}, "expectations": {...} | omitted}]` in a deterministic order and
  never rewrites records.
- `exists` is side-effect free. `native` is optional and must not import a vendor SDK until called.
- Frozen datasets are enforced by core (`EVALOPS_ALLOW_FROZEN_CHANGE` is the only override); plugins
  must not bypass or reinterpret it.

### 3.3 `Ledger`

```python
start_run(name: str, tags: dict) -> str
end_run(run_id: str, status: str = "FINISHED") -> None
log(run_id: str, params=None, metrics=None, tags=None) -> None
put_artifact(run_id: str, name: str, text: str) -> None
get_artifact(run_id: str, name: str) -> str
find_runs(tags: dict | None = None, since_ms: int | None = None,
          limit: int | None = None) -> list[RunRecord]
```

Invariants:

- `find_runs` is **AND semantics**: a run matches only if every supplied tag key/value matches.
  Unknown tag keys match nothing.
- Results are **newest-first** by `start_ms` descending; ties break by `run_id` descending so the
  order is total and deterministic.
- `since_ms` is inclusive. `limit` applies after sorting; `limit=None` returns all matches.
- Artifacts are immutable per `(run_id, name)`: `put_artifact` with the same pair may be idempotent
  with identical text and must raise on different text. `get_artifact` returns exact text.
- `start_run`/`end_run`/`log` never delete or rewrite a completed run. Baselines are selected by
  tags (`evalops.baseline=true`), so tag semantics are part of the contract.

#### 3.3.1 Budget reservation lifecycle (WP1.2)

The budget hooks (`budget_reserve/settle/release/reserved`, added in WP1.2) are an **optional**
ledger capability with an explicit lifecycle:

- **Atomic accounting is a SQL-ledger capability; non-SQL ledgers are advisory-only.** The `Ledger`
  defaults track nothing: `budget_reserve` returns `True`, `budget_settle`/`budget_release` are
  no-ops, and `budget_reserved` returns `0.0`. A caller (or a test) must not read a successful
  reserve on such a ledger as a tracked reservation, and must not expect a refusal (exit 3) from it.
- **A concurrent first reserve in a month with no row either reserves or refuses with exit 3; it
  never fails with a backend constraint error.** The SQL ledger creates the month row and applies the
  delta in one upsert statement (`reserved_usd`/`settled_usd` re-add the delta, not "do nothing"), so
  N racing first reservations of a fresh month all count. The spelling is dialect-specific: SQLite
  and Postgres use `INSERT ... ON CONFLICT (month) DO UPDATE`, MySQL/MariaDB use
  `INSERT ... ON DUPLICATE KEY UPDATE reserved_usd = reserved_usd + :r, settled_usd = settled_usd + :s`
  (the same re-apply-the-delta shape; MySQL 8.0.20+ deprecates `VALUES(col)`). **MySQL/MariaDB budget
  reservation is untested** in this repo: there is no MySQL contract service, and Postgres is the
  verified non-SQLite backend. The other ledger roles (runs, kv, artifacts, `migrate`, `verify`) use
  portable SQL and are unaffected. Any other dialect raises `CapabilityError` (exit 6) once, before
  the first budget write; the SQL ledger never guesses a dialect's upsert spelling.
- **A run that crashes between reserve and settle leaves its reservation counted** against that UTC
  month until it is released. The ledger stays self-consistent, so `evalops ledger verify` does not
  flag it, and there is no CLI repair command yet: release the reservation (or correct the amount)
  with a manual SQL update on `evalops_budget` and `evalops_budget_month` together — changing only
  one of them is exactly the `budget_mismatch` that `verify` reports — until a future `evalops ledger
  release` lands.
- **`evalops ledger migrate` is the intended first step.** A database with no
  `evalops_meta(key='schema_version')` row is schema version 0, which covers both a fresh and a
  legacy pre-WP1.2 database. `verify` checks whatever tables exist and reports `schema_version: 0`;
  the budget tables are also created lazily by the first budget operation *without* stamping the
  version, so `migrate` still reports step 1 as pending on such a database.

### 3.4 `Tracer`

```python
wrap(fn: Callable, *, run_id: str, row_id: str) -> Callable
usage(trace_ids: list[str]) -> dict[str, Usage]     # optional hook
flush() -> None                                     # optional hook, idempotent
```

Invariants:

- The wrapped callable has the same call signature as `fn` and returns `(output, trace_id)`.
- Exceptions from `fn` propagate unchanged; the tracer must not swallow or rewrite them.
- A no-op tracer returns `(output, None)`; a missing trace is not an error.
- `usage` may return partial data; missing usage is reported, never invented.

### 3.5 `Evaluator`

```python
evaluate(*, records, dataset_physical, dataset_store, predict_fn, scorers,
         tracer, run_id, run_name, concurrency, stored_rows=None) -> EvalResult
```

Invariants:

- Calls `predict_fn` once per record (or re-scores `stored_rows` when supplied) and returns one
  `RowResult` per record in a stable order. Per-row failures are captured on the row (`error`),
  not raised; the run continues.
- `EvalResult.metrics` keys are stable and backend-independent. Row order and metrics must not
  depend on concurrency settings.
- The evaluator does not register prompts or write datasets; it only reads its inputs.
- Core (`plugins/core.py`) is the reference implementation for any native evaluator adapter.

## 4. Identity hashes (semantic fields only)

Canonicalization is `json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)`.

| Hash | Covers | Never covers |
|---|---|---|
| `dataset fingerprint` | sorted per-row canonical `{inputs, expectations, outputs, trace}` (empty/missing expectations normalized to `None`) | row ids, file paths, tags, timestamps, `schema_version`, `evalops_version`, comments, source line order |
| `physical dataset name` | `<name>__<fingerprint[:8]>` | backend ids, environment label |
| `cassette fingerprint` | sorted canonical entries over the semantic entry fields (`tool`, bound args, result) | `_meta`, `schema_version`, `evalops_version`, `created_at`, record order |
| `app_version` | `<project>-<git_sha8>-<cfg_hash8>` where `cfg_hash` covers behavior config + `name@version` prompt refs + `judge_meta` + plugin map + cassette fingerprint/mode | timestamps, absolute paths, actor, env label (recorded separately), `schema_version`, `evalops_version`, metadata |

Rules:

- **Adding metadata must not change an identity hash.** Stamping `schema_version`/`evalops_version`
  into a lock, row, cassette, or manifest is metadata-only (risk R1).
- Prompt identity is `name@version`, not the template text (the backend owns content).
- Row identity is stable: dataset fingerprint + the row's semantic key. v0 rows use a zero-padded
  index (`00000`); the contract adds a stable row key additively, so reordering a dataset must not
  silently re-associate stored results (WP1.3).
- WP0.4 proves invariance with golden tests: v0 fixture parses, v1 round-trips, and metadata
  stamping leaves `app_version` and cassette fingerprints byte-identical.
- `row_key` (WP1.3) is an **additive** per-row field in `evalops/rows.jsonl`: it is a consumer of the
  identity rules above, never an input to any identity hash. It never enters `dataset fingerprint`,
  `app_version` or the cassette fingerprint; a v0 rows artifact without it still reads (the gate then
  falls back to `row_id`). §9 fixes its exact definition.

## 5. Persisted-format policy

Every **machine-read** artifact evalops writes carries `schema_version` (`FORMAT_VERSION = 1`) at the
documented location below (WP0.4; `evalops/cost_estimate.json` added in WP0.4b). Human-facing
output (`.evalops/out/gate_report.md`, stdout, the `--json` envelope of §2) is not a persisted
machine artifact and carries no `schema_version` of the format kind — the envelope has its own.

Versioned artifacts:

| Artifact | Location of the version |
|---|---|
| `.evalops/locks/<env>/prompts.lock.json`, `datasets.lock.json` | reserved `_meta.schema_version` |
| `cassettes/<dataset>.jsonl` | first line `{"_meta": {...}}` |
| `evalops/rows.jsonl` artifact | per line |
| `evalops/rows.jsonl` `trace` field (nested trace document) | nested `trace.schema_version` (`TRACE_SCHEMA_VERSION = 2`, WP1.5 §5.6) |
| `evalops/cost_estimate.json` artifact | top level |
| `.evalops/out/last_eval.json` | top level |
| `evalops/manifest.json` artifact | top level (`evalops_version` too; it lists the hashed artifacts) |

Ledger **params/tags** (not files) record `evalops_version` and `format_version` on each run for
diagnosis; they are additive and never used for compatibility decisions.

The **ledger table schema** has its own version, separate from `FORMAT_VERSION` and from the
artifact versions above: `LEDGER_SCHEMA_VERSION` in `src/evalops/plugins/sql.py`, recorded in
`evalops_meta(key='schema_version')` (a database with no row is version 0). `evalops ledger migrate`
applies pending steps idempotently and preserves rows; a recorded version greater than the reader's
is the same hard stop as a future artifact version (exit 7), never a silent downgrade.

Policy:

1. **Additive only.** New fields may be added in a minor release; fields are never renamed, retyped,
   or repurposed in place. Removal requires a major version and a migration command.
2. **One-minor read compatibility.** A v1 reader accepts v0 (unversioned) files; a v2 reader will
   accept v1 files. Files are never rewritten in place to a different schema; migration writes a new
   artifact.
3. **Unknown future version is a hard stop.** A `schema_version` greater than `FORMAT_VERSION`
   raises `FormatError` → **exit 7**, naming the file, the found version, and the supported maximum.
   Silent best-effort parsing is forbidden.
4. **Missing `schema_version` means v0.** It is read as the legacy format, never guessed as current.
5. **Writers emit `FORMAT_VERSION` only.** `evalops_version` records the writer build for diagnosis,
   not for compatibility decisions.
6. Every format change ships with: version bump, a v(n-1) read test, a v1 round-trip test, and a
   fingerprint-invariance test. A format change without a migration/read test is a failed WP.
7. Reserved keys (`_meta`, and any key starting with `_`) are never prompt/dataset/tool names.

### 5.1 Redaction (WP1.1)

Payload redaction is one hook ([`src/evalops/contract/redact.py`](../src/evalops/contract/redact.py))
applied at exactly **three** outbound boundaries, always **after** the identity hashes of §4 are
computed:

| Boundary | What is redacted |
|---|---|
| `run._put_artifact` | every ledger artifact body (`evalops/cost_estimate.json`, both locks, `evalops/rows.jsonl`). `evalops/rows.jsonl` is line-delimited JSON and takes the JSONL path, so `evalops/manifest.json` hashes the *stored* (redacted) bytes. |
| `AgentTrace.to_dict` | a redacted deep copy of the emitted document; the in-memory trace (scorers, `render`, replay) is unchanged. |
| `Cassette.save` | a redacted deep copy of the entries written. `Cassette.entries` and `Cassette.fingerprint()` are unchanged by `save()`, and `_meta` gains `redacted: true` and `redaction_rules: <sha256[:12]>` **only** when a rule actually changed something. `FORMAT_VERSION` stays `1`: the keys are additive metadata. |

Nothing else is a redaction boundary: `.evalops/out/last_eval.json` is written from run metadata (no
row payloads), so it carries no redacted *row* field -- the only diagnostic it can carry is the
already-redacted replay-miss label described next. Replay-miss diagnostics (a replay miss under
`tools.on_miss: live` or the miss error) are **redacted at capture time** with the same rules, so
`.evalops/out/last_eval.json`, `data.tool_replay_misses` and the miss error message carry the redacted
label, never the raw tool arguments; this is not a fourth boundary, it happens before any sink. Only
`patterns`/`hook` apply to that label -- `fields:`/`hash:` name dotted paths inside a document and
cannot match a flat string. Redaction covers **run payloads** -- ledger
artifact bodies, trace serialization, cassettes -- and not configuration provenance or ledger
params (`behavior_config`, the `app_version` hash inputs, run names/tags stay unredacted); secrets
must therefore not live in `evalops.yaml` or in a `behavior_config()` payload.

A cassette saved with redaction is **lossy by design**: replay returns the redacted values, and a
reloaded cassette's `fingerprint()` differs from the pre-save fingerprint exactly when redaction
changed a semantic entry field. That is the consequence of §4 (identity hashes cover the semantic
fields and never `_meta`, so a `_meta`-provided identity would be spoofable); a cassette whose
identity must survive record/replay round-trips uses `mode: off` or hash-only rules.

Config: `redact:` is a closed section (`_Strict`), so `redact.<key>` is exit 2. Keys: `mode`
(`strict` | `off`), `fields`, `hash`, `patterns` (`{name, regex, replace?}`), `hook`
(`module:attribute`, validated like every other `HookSpec`). An invalid `regex` is exit 2 naming the
pattern.

Invariants:

1. **Order.** `fields` (dotted-path globs → `[REDACTED:<glob>]`), then `patterns` (regex substitution
   on every string leaf, default replacement `[REDACTED:<name>]`), then `hash` (dotted-path globs →
   `sha256:<first 12 hex of sha256(canonical(value))>`), then `hook` (`(path, value) -> Any | None`;
   `None`, or an equal value, leaves the node unchanged). Path syntax: `inputs.password` (dict keys),
   `*` one segment, `**` any depth (including zero), `[]` every list element
   (`spans[].outputs.token`). A glob that matches nothing is legal. Redaction covers *values*, never
   dictionary keys.
2. **Idempotence.** `redact(redact(x)) == redact(x)` for `fields`, `patterns` and `hash` always: a
   value already shaped like `[REDACTED:<rule>]` is never re-replaced and a value already shaped like
   `sha256:<12 hex>` is never re-hashed. A `hook` must be idempotent itself and must return `None`
   for a value it already produced.
3. **No identity hash moves.** Redaction runs after `cfg_hash`/`app_version` and the cassette
   fingerprint are computed, and the new `_meta` keys are metadata: a run with redaction enabled has
   the same `app_version`, dataset fingerprint and cassette fingerprint as the same run with it off.
4. **Modes.** `redact.mode` defaults to `off`, which is bug-compatible: every byte is what evalops
   wrote before this section existed. `EVALOPS_REDACT=strict|off` overrides the configured mode; any
   other value is exit 2 naming the legal values (§1).
5. **`strict` is a real write-path refusal.** After redaction the produced payload is re-scanned
   with the same rules, and anything that still matches aborts with **exit 5** *before* a byte is
   persisted (no artifact, no manifest, no cassette). It is never a warning. Artifact bodies are
   buffered until every one of the run's payloads -- `evalops/cost_estimate.json`, both locks and
   `rows.jsonl` -- has been verified, so a refusal writes *no* artifact, not a prefix of them. The
   manifest is derived metadata and is never redacted; its hashes cover the stored artifact bytes.
6. **`evalops redact` is a dry run.** `--check` is accepted but optional and changes nothing:
   `evalops redact` and `evalops redact --check` behave identically (report only, never write). It
   reads the ledger artifacts of a run group (default:
   the group in `.evalops/out/last_eval.json`; exit 2 when neither exists) and/or repeatable
   `--file PATH` (`.jsonl` = cassette/rows, `.json` = document), renders a line-oriented unified diff
   (human mode) or counts plus at most 50 changed paths per sink (`--json`, inside `data`: no new
   top-level envelope key), and **never writes**. It exits **0 even when it finds differences**: the
   §1 exit matrix is frozen and "found a secret" is not a gate outcome. Because it is a command added
   after §2 was frozen, its envelope is pinned by an additive golden
   (`tests/fixtures/envelope/redact.json`) and the §2 golden list for the pre-existing commands is
   unchanged.
7. **Serialization-normalized.** Every payload is normalized through the same
   `json.dumps(..., default=str)` round-trip the sinks write with *before* it is walked: a tuple
   becomes a list, a set/dataclass/custom object becomes `str(value)`. The tree that is scanned is
   therefore bit-for-bit the tree that is serialized, so a secret carried by a non-JSON value is
   redacted (and refused by `strict`) exactly like one in a dict/list.

### 5.6 Nested trace document (`evalops/rows.jsonl` `trace`) — WP1.5, additive

The normalized agent trace is a **second, independently versioned document** nested in every rows
line (§4 rules apply unchanged; this is an additive §5 row, not an invariant change):

- **Identifier.** `TRACE_SCHEMA_VERSION = 2`, in `src/evalops/traces/model.py` and exported from
  `evalops.traces`. It is separate from `FORMAT_VERSION`, which still versions the rows line.
- **Writers emit 2 only.** `AgentTrace.to_dict()` always writes `"schema_version": 2`; a document
  read as v1 (or unversioned) is therefore upgraded on the next write.
- **Readers accept the past.** An **absent** `trace.schema_version` is **v1**, the legacy shape
  written before this row existed (rule 4: a missing version is the legacy format, never guessed as
  current). An explicit `1` or `2` is accepted and the object records the version it read.
- **Anything else is exit 7.** `3+`, `0`, a string, `null` and a bool raise `FormatError` naming the
  artifact and the supported maximum (`evalops/rows.jsonl`, or `dataset:<name>` on the calibration
  path), exactly as rule 3 requires. Unknown top-level and unknown span keys are ignored, never a
  `TypeError`.
- **Metadata only for rows and row identity.** The trace version never enters `app_version` or
  `row_key`, and the same trace as v1 and as v2 yields identical `trace_metrics` and identical gate
  checks. A **trace-less** dataset's fingerprint is unchanged. A dataset **source record that
  embeds a trace document** is fingerprinted verbatim (§4), so upgrading that embedded document
  changes the dataset fingerprint by design: it is a new dataset snapshot, not a metadata-only
  change.
- **Unmodeled attributes are reported by name.** `Span.unknown_attributes` lists the *names* of raw
  attributes `normalize()` did not consume (sorted, deduplicated — never values, which may be
  secrets), and `AgentTrace.unknown_attributes()` aggregates them as `{name: span_count}`.
  `evalops traces --unknown-attrs` is the only surface for them, so the default `traces` payload
  (and its envelope golden) is unchanged.

---

## 6. Judge pinning

D11 applies to every judge used in `plan`, `eval`, `calibrate`, and `gate`. The rules below are the
normative target; **enforcement lands in WP3b.5** (gate refusal, resolved-model recording per
verdict, nightly server-side settings check, calibration as a gate input). Until that WP ships, a
rule is binding on new code but only enforced where a test exists.

- The pin is `judges.<name>.model` in `evalops.yaml`. It must be a concrete model identifier.
  Defaults, aliases (`latest`, `-latest`, a project default), and empty values are not pins.
- `judge_meta()` may **add** metadata (rubric, prompt version, sampling, cost) but must never
  override the model or prompt version from config. A conflict is exit 2 at load.
- Every verdict records the judge name, resolved model, and `judge_prompt_version` on the run. A
  verdict without a resolved model is a contract violation. *(target: WP3b.5)*
- An unpinned judge **fails the gate (exit 1)**; it is never a warning. Loading an unparseable pin
  is exit 2. *(target: WP3b.5)*
- Judge changes ship separately from app changes, bump `judge_prompt_version`, and must pass
  `evalops calibrate` against human labels before gating. Calibration below
  `judges.<name>.min_agreement` / `min_kappa` **fails the `pipeline pr` / `pipeline main` stage**
  (the `calibrate` step exits 1 and the stage error names the judge, metric, required limit and
  measured value — WP2.5); consuming calibration as an input of the `gate` command itself is
  **WP3b.5**.
- Server-side judge settings (Langfuse project defaults, pydantic-evals defaults) are re-checked in
  the nightly job; drift fails nightly. A wrapper records the **resolved** model per verdict (R3).
  *(target: WP3b.5)*
- **A declared judge spec is validated at load (WP3b.1).** A `judges.<name>` block that carries any
  of `rubric`, `scale`, `prompt_version`, `sampling` declares a spec, and an unpinned or
  alias/default value is **exit 2 at load**, naming the dotted key (`judges.<name>.model`):
  `default`, `latest`, `*-latest`, `auto`, `default/*`, `*/*/default` and empty are not pins.
  `scale.kind` is closed (`binary` | `ordinal` | `continuous`), `min < max`, `binary` is `0..1`, a
  `threshold` lies inside the scale, and sampling bounds are `temperature` in `[0, 2]`,
  `max_tokens > 0`, `seed` an int or null. Numeric validation runs in two layers, both exit 2 (the
  config model at the exact key, `JudgeSpec.validate()` at the owning path), and neither layer may
  emit `Infinity`/`NaN` in an envelope. `evalops judges --json` reports the identity surface per
  spec (`rubric_hash`, never the rubric text) and the `unpinned` worklist. A block that declares no
  spec (thresholds and/or `model` only) is unchanged by WP3b.1: **validating the pin for every
  judge, and refusing a `judge_meta()` that disagrees with the config pin, remain WP3b.5.**
  `unpinned` is therefore not an exhaustive list of unvalidated judges.
- **Native judge adapters exist per evaluator (WP3b.2).** `scorers/native.py` declares exactly one
  adapter per evaluator backend (MLflow `make_judge` for `mlflow`, managed evaluators with
  server-side execution + score read-back for `langfuse`, openevals for `langsmith`, pydantic-evals
  for `core`; `databricks` rides on `mlflow`). Every adapter imports its SDK lazily inside a
  function body and a missing SDK is `CapabilityError` → **exit 6** naming the pip package, as is
  asking an evaluator with no adapter (CONTRACT §3.0/§7.4); results normalize through `to_float`
  and a missing vendor score is `None`, never `0.0`. The vendor call shapes are `VERIFY`-marked
  and resolved in phase 3. Nothing wires a declared spec into `plan`/`evaluate`/`calibrate` yet —
  auto-selection (native where available, the LiteLLM rubric fallback where declared) is **WP3b.3**;
  resolved-model recording per verdict and gate-time pin enforcement remain **WP3b.5**.

## 7. Third-party plugin requirements

A plugin is installable independently; `evalops` core has no vendor dependency on it.

1. **Packaging.** Expose one or more classes via the entry-point group `evalops.plugins`:

   ```toml
   [project.entry-points."evalops.plugins"]
   mybackend = "evalops_mybackend:MyBackendPlugin"
   ```

   `<module:Class>` specs are also accepted for local development. The distribution has its own
   name/version and depends on `evalops` with a compatible major version.
2. **Ports.** Subclass `Plugin` plus the role ABC(s) implemented, set `name` and `provides`, and
   implement every abstract method of those roles. All invariants in §3 apply.
3. **Exit codes.** Raise `CapabilityError(plugin, operation, hint)` for unsupported operations →
   exit 6. Raise `EvalOpsError(msg, code)` for recoverable failures. Never call `sys.exit` and never
   let a vendor exception escape unwrapped.
4. **Import hygiene.** Importing the plugin module is side-effect free and must not require the
   vendor SDK to be installed at import time; import it lazily and translate `ImportError` into a
   `CapabilityError` hint naming the install extra.
5. **No bypass.** The plugin never calls evalops-internal lock/store paths, never edits frozen
   datasets, and never promotes aliases on its own.
6. **Conformance.** `evalops plugin test <module>` (WP6.3) runs the shared suite:
   register-never-overwrites, dataset write-once, `find_runs` AND + newest-first, alias sibling
   preservation, and unsupported-op → exit 6. A plugin is "contract-verified" only when that suite
   is green; otherwise it is "generated".
7. **Formats.** Plugin-side artifacts follow §5; any new persistent shape needs a schema version and
   a read test in the plugin's own repo.
8. **Offline CI.** The plugin's own tests use recorded fixtures and must pass without credentials.

## 8. Conformance checklist (what a reviewer can test now)

- [ ] Exit codes `0`–`7` are exercised; `7` is produced only on read of a future format.
- [ ] `--json` output is a single document; `ok`, `code`, `schema_version`, `command`, `env`, `data`,
      `errors` follow §2 (golden snapshots from WP0.5).
- [ ] Registering the same prompt twice never changes version `v1`'s content; sibling aliases survive
      a `set_alias`.
- [ ] Editing a dataset creates a new physical snapshot and never rewrites the old one.
- [ ] `find_runs` returns AND-filtered, newest-first, deterministic results.
- [ ] A plugin lacking a role yields exit 6 with a hint.
- [ ] Metadata/schema stamping does not change `app_version`, dataset fingerprints, or cassette
      fingerprints.
- [ ] A future `schema_version` yields exit 7 naming file/version.
- [ ] No vendor import exists outside `src/evalops/plugins/` and `src/evalops/traces/`
      (`tests/unit/test_no_vendor_imports.py`).
- [ ] An unpinned judge fails the gate; a resolved model is recorded per verdict.

## 9. Statistics and gate diagnostics (WP1.3, additive)

This section defines the numbers the gate reports. It is additive: no rule above changes meaning,
no existing key is renamed, and every diagnostic below is reported in the `--json` envelope and
`gate_report.md` **without** altering any check outcome.

### 9.1 Dispersion, and the two confidence intervals

- **Standard deviation is the sample (n-1) one.** `std` keeps its name and now means
  `statistics.stdev(vals)` everywhere evalops reports dispersion; `n == 1` reports `0.0` instead of
  raising. No gate rule reads `std` implicitly: every threshold is an explicit configured key, so a
  rule's meaning never changes with this convention.
- **`ci_lower` (the `min_ci_lower` rule) is the normal approximation:**
  `mean - z * sample_stdev / sqrt(n)` with `z = 1.96`, `None` below `n == 2`. This is unchanged.
- **`wilson_interval(successes, n, z=1.96)`** is the proportion interval, reported as
  `{lower, upper, method: "wilson", n, k}`; `None` when `n == 0`. It is used for the proportions the
  reliability block reports (`pass_hat_k`, `pass_at_k`, `success_rate`) and stays inside `[0, 1]`,
  with `lower == 0.0` at `k == 0` and `upper == 1.0` at `k == n`.
- **`bootstrap_ci(vals, resamples=2000, seed=0, alpha=0.05)`** is a
  `{lower, upper, method: "bootstrap-percentile", resamples, n}` percentile bootstrap of the mean:
  deterministic for the same input and seed (the RNG is local to the call), `None` below two values.
- **`min_ci_lower_bootstrap`** is a new optional per-metric rule that uses the bootstrap interval's
  lower bound; it never replaces `min_ci_lower`, whose meaning is frozen above.

### 9.2 pass^k: exact semantics

For every case (identified as in §9.3) the success vector is ordered by the run's `evalops.repeat`
**as an integer**; when that order cannot be established (missing, non-numeric or duplicate repeat
tags) the group's positional order is used and the result says `unordered_repeats: true` — a string
sort would place repeat `"10"` before `"2"`, which is not an order.

With `k = min(len(vector))` over all cases, `C` cases and `N` total repeats:

- `pass_hat_k = (1/C) * sum(all(vector[:k]))` — succeeds on **every one** of the first k repeats.
- `pass_at_k = (1/C) * sum(any(vector[:k]))`.
- `success_rate = total successes / N`.

This is the **plug-in estimator**: "all k observed repeats", not the combinatorial
"at least k of n". A case cannot have fewer repeats than `k` by construction; the block still
reports `cases_with_fewer_repeats` (cases missing from at least one run) for transparency and sets
`comparable: false` when it is non-zero, because a ragged matrix compares different denominators.

- **`min_pass_hat_k_wilson`** (WP1.3b) is an optional reliability rule that gates the *lower* end of
  the `pass_hat_k` Wilson interval instead of the point estimate: the check is
  `pass_hat_k_ci.lower >= <threshold>` and fails when the interval is missing. `min_pass_hat_k` and
  `max_drop_pass_hat_k` keep their point-estimate meaning.

### 9.3 Row identity

`row_key = sha256(canonical({"inputs": ..., "expectations": ...}))[:16]`, with `expectations`
normalized exactly like the dataset fingerprint (missing/empty → `None`). Metadata, backend ids and
recorded `outputs`/`trace` never reach it.

Identical rows are interchangeable, so a duplicated key gets a deterministic occurrence suffix by
file order (`<key>#1`, `<key>#2`, …); a unique row carries the bare key. Reordering *distinct* rows
therefore changes no key.

`row_key` is written additively on every row of `evalops/rows.jsonl` (evaluate **and** re-score
paths; calibration rows too) and read back as `RowResult.row_key`. Row-level statistics
(`reliability`, `paired_drift`) align on `row_key` **only when every row in every run carries one**,
and otherwise fall back to `row_id`, reporting `aligned_by: "row_id"`. A v0 rows artifact without
`row_key` therefore still gates, just positionally.

### 9.4 Baseline pinning

`evalops promote --alias production` tags each new baseline run with
`evalops.baseline.app_version` and `evalops.baseline.dataset_fingerprint`, read from that run's
params. `run_gate` reports `baseline: {group, app_version, dataset_fingerprint, pinned}` and adds
two **failing-only** checks:

- `baseline dataset fingerprint matches` — the candidate's `dataset_fingerprint` differs from the
  baseline's: comparing different data is not a drift measurement.
- `baseline pin matches run` — a baseline that carries pin tags disagreeing with its own params was
  moved or tampered with.

No baseline → no check, exactly as before. Both checks are absent when there is nothing to compare.

### 9.5 Drift and change attribution

`drift(cand_runs, base_runs, rules)` reports, for every metric a rule names and every metric present
in both aggregates: `delta`, `pct_change`, `cohen_d` (pooled sample sd), `z = delta /
sqrt(se_c^2 + se_b^2)` with `se = sample_stdev / sqrt(n)`, the two-sided `p = erfc(|z| / sqrt(2))`
(no scipy), and `significant = p < 0.05`. `z`/`p`/`cohen_d` are `None` when the runs give no
dispersion to test against.

`paired_drift` adds per-row-key pairing for metrics that have row values on both sides: `aligned`,
`unaligned`, `mean_delta`, `ci` (bootstrap) and `significant`. It is only included in `drift` when
alignment is by `row_key`; a `row_id` alignment is positional and pairing it would invent a
correspondence.

`attribution(cand_runs, base_runs)` compares the newest run of each group over identity/provenance
params only (`app_version`, `config_hash`, `git_sha`, `git_dirty`, `dataset`,
`dataset_fingerprint`, `dataset_physical`, `tool_mode`, `tool_cassette`, `source_group`, plus every
`prompt.*`, `judge.*` and `plugin.*` key), as strings. `behavior_config` is excluded (it is truncated
JSON, so its diff is noise). The result is `{changed: {param: {candidate, baseline}}, summary}`; it
contains identifiers only, never row payloads.
