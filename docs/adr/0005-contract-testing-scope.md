# ADR 0005: Contract-testing scope

- Status: Accepted
- Date: 2026-10-23
- Decision: D7

## Context

The vendor plugins follow current SDK documentation but several API details are unverified (30+
`VERIFY` lines, e.g. `mlflow.result_df` shape, `langfuse.api.trace.get`, LangSmith prompt commits,
Logfire's private SQL). Live credentials for LangSmith, Logfire, and Databricks are not reliably
available to CI. At the same time the offline suite must stay green on every PR and must never
import a vendor SDK.

## Decision

1. Contract-test **docker-local only** first: MLflow + Postgres and Langfuse + Clickhouse via
   `docker-compose`, selected with the `contract` pytest marker.
2. Keep **LangSmith, Logfire, and Databricks** in `beta` until a contract test runs green with real
   credentials. Beta is user-visible (`doctor`, capability docs, exit 6 hints).
3. Record HTTP/API fixtures for the tested flows so the default CI suite runs offline from fixtures
   (`-m "not contract"` is the default in `pyproject.toml`).
4. A backend may be called "contract-verified" only when a test asserts the behaviour; documentation
   claims without a test are not evidence.

## Consequences

- Every PR stays offline-green; vendor verification runs in a nightly docker job.
- Unverified paths are marked `beta` with a hint instead of being presented as verified (R2).
- Recorded fixtures become versioned test assets and must be refreshed when an SDK is upgraded.
- The set of supported backends is explicit and derived from evidence (supports the support tiers in
  Phase 4).
- Resolving the remaining `VERIFY` lines requires either docker services or user credentials; until
  then those lines stay visible in code and in the capability matrix.
