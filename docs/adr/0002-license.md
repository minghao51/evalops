# ADR 0002: Apache-2.0 license

- Status: Accepted
- Date: 2026-10-23
- Decision: D2

## Context

evalops is a glue layer that will be embedded in other teams' CI pipelines and integrated with
several vendor SDKs. Integrators need patent clarity and a license compatible with the surrounding
CI/plugin ecosystem. A copyleft license would discourage the plugin ecosystem we depend on.

## Decision

**Apache-2.0 is the decision** for this project. It is settled here and is not revisited by the WPs
below; only its packaging delivery is deferred.

Delivery status: **not shipped yet.** The repository has no `LICENSE` file and `pyproject.toml`
carries no `license`/`license-files` metadata today; both land in **WP6.1** (publish WP). Until
then, treat this ADR as the binding intent and do not assume an installable license artifact.

When delivered (WP6.1), ship the full license text as `LICENSE`, keep the project metadata
(`pyproject.toml`, README) consistent with `Apache-2.0`, and include a `NOTICE` only if third-party
code is vendored (none is planned).

## Consequences

- Explicit patent grant and a patent-retaliation clause protect both users and contributors.
- Apache-2.0 is compatible with the ecosystem's common permissive licenses and with commercial use.
- Contributions are accepted under the same license (inbound = outbound); no CLA initially.
- Third-party plugin packages choose their own license; core remains Apache-2.0 and does not link
  their code at build time.
- License headers are optional; the repository-level `LICENSE` (WP6.1) is authoritative.
