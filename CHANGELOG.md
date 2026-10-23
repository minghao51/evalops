# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: semver, with
`FORMAT_VERSION` for persisted artifacts documented in `docs/CONTRACT.md`.

## [Unreleased]

### Added

- `docs/CONTRACT.md`: frozen exit codes (0–7), `--json` envelope v1, the five plugin port
  invariants, identity-hash rules, persisted-format policy, judge pinning policy.
- ADRs 0001–0007 (naming, license, pydantic in core, phase order, contract-test scope, judge
  pinning, exit codes + format versioning).
- `evalops.contract.formats` (`FORMAT_VERSION = 1`): versioned locks, cassettes, `rows.jsonl`,
  `last_eval.json`, `cost_estimate.json`, and an `evalops/manifest.json` artifact with per-artifact
  hashes. v0 artifacts still read; an unknown future version exits 7 naming the file.
- Semantic-only identity hashes: cassette and dataset fingerprints and `app_version` ignore
  metadata, so stamping an artifact cannot invalidate recorded runs (pinned test constant
  `73357e6c4b54`).
- Dev loop: `tests/{unit,integration}` split, recursive pytest-free runner, ruff + mypy (strict on
  `contract`/`config`), `make check`, GitHub Actions matrix 3.10–3.12, and an AST guard proving the
  core never imports a vendor SDK (with its limits pinned by tests).

### Changed

- Exit code `7` (`CONTRACT`) added for format mismatches, raised on read only.
- Lock files carry a reserved `_meta` block; readers go through `locks.entries()`.

## [0.3.0] - baseline

The inherited v0.3 codebase: five plugin roles, seven environments, agent trace normalization,
trajectory scorers, tool cassettes, reliability gating (pass^k), judge calibration, and a bash CI
pipeline. 17 tests.
