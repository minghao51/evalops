# ADR 0004: Phase order (3b before 3)

- Status: Accepted
- Date: 2026-10-23
- Decision: D6

## Context

The roadmap has two adjacent bodies of work:

- **Phase 3b — judge adapters**: `JudgeSpec`, native judge wrappers (MLflow `make_judge`, Langfuse
  managed evaluators, openevals, pydantic-evals), the LiteLLM fallback, pairwise/panel wrappers, and
  pin enforcement. This is pure code that can be unit-tested against fake SDKs.
- **Phase 3 — vendor contract verification**: docker-compose services, credentials, resolving 30+
  `VERIFY` API lines, and lineage backlinks.

Phase 3 needs live services and credentials that are not reliably available, while the **gate cannot
be trusted without pinned, calibrated judges**. Blocking the gate on Phase 3 would stall the
critical path behind infrastructure.

## Decision

Order the phases **0 → 1 → 2 → 3b → 3 → 4 → 5 → 6**: judges (3b) land before vendor contract
verification (3). A judge adapter is "done" when its unit tests pass against a fake SDK and the
capability matrix declares which presets have native judges. Phase 3 then verifies the real APIs.

## Consequences

- The gate becomes usable (pinned + calibrated judges) earlier, at G3b / v0.5.0.
- Vendor verification is delayed but keeps its own gate (G3) and its own evidence standard.
- Phase 3b adapters must be written defensively so a later real-SDK check only confirms, not
  redesigns, the wrapper.
- Reordering again requires a new ADR and an orchestrator entry in `IMPLEMENTATION_MAP.md`.
- Release cadence follows the order: v0.4.0 at G2, v0.5.0 at G3b, v1.0.0 at G6.
