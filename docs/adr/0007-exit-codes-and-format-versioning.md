# ADR 0007: Exit codes and persisted-format versioning

- Status: Accepted
- Date: 2026-10-23
- Decision: D12

## Context

Agents and CI branch on exit codes, so the mapping must be stable forever. Separately, evalops
persists locks, rows, cassettes, `last_eval.json`, and artifact manifests. Introducing a
`schema_version` naively would change every identity hash and invalidate all recorded runs (R1,
R6). A reader that guesses at a future format can silently mis-score data.

## Decision

### Exit codes

- Freeze `0` ok, `1` gate/calibration failed, `2` usage/config, `3` budget, `4` guard, `5` runtime,
  `6` unsupported by the configured plugin. **Never renumber.**
- Add **`7` = contract/format mismatch**: a persisted artifact (or envelope) declares a version this
  evalops does not understand. It is raised on read only.
- New codes are append-only and require an ADR.

### Persisted formats

- `FORMAT_VERSION = 1`; every artifact carries `schema_version` at a documented location.
- Readers accept the current and the previous version (v0 is "no `schema_version`"); unknown fields
  are ignored. Writers emit the current version only.
- `schema_version` greater than `FORMAT_VERSION` is a hard stop → exit 7, naming the file, found
  version, and supported maximum.
- Changes are additive. Renames/removals require a major version and a migration command.
- Identity hashes cover **semantic fields only**; metadata stamping must not change dataset
  fingerprints, cassette fingerprints, or `app_version`.

## Consequences

- `7` can never be reused for another meaning; callers can distinguish "upgrade evalops" from "the
  backend is broken".
- Readers must branch on versions and keep v(n-1) fixtures; every format change ships with a
  v(n-1) read test, a round-trip test, and a fingerprint-invariance test.
- Metadata (`_meta`, `schema_version`, `evalops_version`, `created_at`, backend ids) must stay out of
  every hash, which constrains how new fields are added to identity-bearing records.
- A future v2 must keep a v1 reader for one minor release; a removal is a major release with a
  migration command.
