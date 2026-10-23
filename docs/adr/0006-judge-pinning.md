# ADR 0006: Judge pinning and resolved-model recording

- Status: Accepted
- Date: 2026-10-23
- Decision: D11

## Context

LLM judges silently drift: a default model alias is repointed, a server-side project setting changes,
or a rubric prompt is edited. Any of these changes scores without changing the app, which makes a
gate result unattributable and can hide a real regression (R3). An unpinned judge that only emits a
warning will eventually gate a release.

## Decision

1. `judges.<name>.model` in `evalops.yaml` is the **single source of the pin**. Concrete model
   identifiers only; aliases/defaults are rejected.
2. `judge_meta()` may add metadata (rubric, prompt version, sampling, cost) but may **never
   override** the configured model or prompt version. Conflicts are exit 2 at load.
3. Every verdict records the judge name, the **resolved model actually used**, and
   `judge_prompt_version` on the run. Verdicts without a resolved model are a contract violation.
4. An unpinned judge **fails the gate with exit 1**, not a warning. Unparseable configuration is
   exit 2.
5. Judge changes ship separately from app changes, bump `judge_prompt_version`, and must pass
   `evalops calibrate` against human labels before they may gate.
6. The nightly job re-checks server-side judge settings and fails on drift.

## Consequences

- Configuration must name a model for every judge used by a gated stage; bootstrapping requires
  explicit pins.
- Run params grow by one `judge.<name>` entry per judge; this is additive and metadata-only, so it
  does not change `app_version`.
- Judge wrapper adapters must expose the resolved model from their backend, even when the backend
  resolves a server-side default (otherwise they cannot satisfy the recording rule).
- Calibration becomes a gate input, not an optional report.
- Pin enforcement and recording land in WP3b.5; the contract is binding on earlier code.
