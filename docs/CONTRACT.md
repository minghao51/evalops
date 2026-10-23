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
| `evalops/cost_estimate.json` artifact | top level |
| `.evalops/out/last_eval.json` | top level |
| `evalops/manifest.json` artifact | top level (`evalops_version` too; it lists the hashed artifacts) |

Ledger **params/tags** (not files) record `evalops_version` and `format_version` on each run for
diagnosis; they are additive and never used for compatibility decisions.

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
  `judges.<name>.min_agreement` / `min_kappa` blocks the gate. *(calibration command exists; as a
  gate input: target WP3b.5)*
- Server-side judge settings (Langfuse project defaults, pydantic-evals defaults) are re-checked in
  the nightly job; drift fails nightly. A wrapper records the **resolved** model per verdict (R3).
  *(target: WP3b.5)*

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
