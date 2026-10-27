# ADR 0008: Evaluator-owned tracing (the tracer role when the evaluator self-traces)

- Status: Accepted (2026-10-27, maintainer decision after the e2e platform run)
- Decision: D12

## Context

CONTRACT §3's five-role model says one plugin serves each role per environment. On the live e2e
platform (dockerized MLflow + Postgres + Logfire, 2026-10-27) three parity gaps surfaced between
the core evaluator and vendor evaluators:

1. Only the core evaluator calls `tracer.wrap` (`plugins/core.py`); the MLflow evaluator serves the
   tracer role **itself** (it declares `tracer` in `provides` and traces rows with native MLflow
   tracing), so a separately configured `tracer:` plugin is silently never invoked.
2. `error_rate` / `budget_exceeded_rate` are emitted by the core evaluator only, so gate rules on
   those metrics fail with "metric missing from candidate" under vendor evaluators — and the
   shipped `mlflow` preset gates on `error_rate` its own evaluator never emits.
3. App-reported usage (`evalops.report_usage`) is harvested by the core evaluator's per-row
   collector; under vendor evaluators every row lands in `rows_missing_usage`.

Forcing vendor evaluators onto the evalops tracer would fight the vendors (their trace UIs, score
read-back, and judge execution are native-first per D-judges) and duplicate tracing. Silently
ignoring a configured role is contract-dishonest.

## Decision

- **Evaluators may self-trace.** When the configured evaluator plugin declares `tracer` in its
  `provides`, it owns row tracing; the separately configured tracer plugin is **not** invoked for
  row tracing. This is documented behavior, not an accident.
- **It must be visible, not silent**: `evalops doctor` reports a warning naming both plugins when a
  self-tracing evaluator is paired with a different configured tracer. A warning, never a failure —
  the wiring is legitimate.
- **Role parity is the evaluator contract, not a core privilege.** `error_rate` and
  `budget_exceeded_rate` are computed by the run layer from `RowResult.error` for **every**
  evaluator; `evalops.report_usage` works under **every** evaluator; the configured tracer's usage
  backfill runs from the run layer for rows still missing usage (a miss stays an honest
  `rows_missing_usage`). The core evaluator keeps no private versions of these steps.
- The capability matrix states which evaluators self-trace natively.

## Consequences

- `run.py` owns metric emission, usage-stash harvesting, and the usage backfill; `plugins/core.py`
  loses its private copies (WP3.0, the parity WP).
- The `mlflow` preset's gate on `error_rate` becomes valid without weakening any gate.
- Vendor row-error mapping (which `result_df` column carries a per-row failure) remains
  `VERIFY`-marked and is resolved in phase 3 (WP3.2) with the live backend.
- A configured tracer that is not invoked for tracing is still invoked for nothing else — the
  warning exists so users stop paying for a tracer role that does nothing in their wiring.
